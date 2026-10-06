"""Configuration models for the standalone AgentDiet/OpenHands runner.

This module intentionally has no dependency on ContextSniper, Native-Agent, or
the original Trae Agent runner.  It only describes configuration; creating the
OpenHands ``LLM`` object is handled by a separate module.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal, Mapping


AuthMode = Literal["subscription", "api-key"]
DietMode = Literal["skip", "ours", "delete", "random", "lingua"]


DEFAULT_OPENHANDS_MODEL = "gpt-5.6-sol"
DEFAULT_OPENHANDS_AUTH: AuthMode = "subscription"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_SUBSCRIPTION_VENDOR = "openai"
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_MAX_ITERATIONS = 500
DEFAULT_REFERENCE_PROFILE = "trae_verified"

DEFAULT_DIET_MODE: DietMode = "ours"
DEFAULT_THRESHOLD_TOKENS = 500
DEFAULT_CTX_BEFORE = 1
DEFAULT_CTX_AFTER = 2
DEFAULT_LINGUA_RATIO = 0.25
DEFAULT_COMPRESSOR_MODEL = "inherit"
DEFAULT_MIN_REDUCTION_TOKENS = 400
DEFAULT_MIN_REDUCTION_RATIO = 0.20
DEFAULT_AGENT_TIMEOUT_SECONDS = None
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120
DEFAULT_VALIDATION_TIMEOUT_SECONDS = 600
DEFAULT_OUTPUT_LIMIT_BYTES = 40_000
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "output"


def resolve_output_root(value: str | Path | None = None) -> Path:
    """Keep CLI/config destinations beneath Agent-Diet/output."""
    root = DEFAULT_OUTPUT_ROOT.resolve()
    if value is None:
        return root
    path = Path(value).expanduser()
    if not path.is_absolute():
        # Accept both --output exp1 and --output output/exp1.
        if path.parts and path.parts[0] == "output":
            path = Path(*path.parts[1:])
        path = root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"output must be a subdirectory of {root}; use --output <run-name>")
    return resolved


def _as_bool(value: Any, *, default: bool = False) -> bool:
    """Parse common boolean representations used by environment variables."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _as_int(value: Any, *, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def _as_float(value: Any, *, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


@dataclass(slots=True)
class OpenHandsConfig:
    """Settings for the OpenHands agent LLM and conversation runtime."""

    model: str = DEFAULT_OPENHANDS_MODEL
    auth: AuthMode = DEFAULT_OPENHANDS_AUTH
    base_url: str | None = None
    api_key_env: str = DEFAULT_API_KEY_ENV
    subscription_vendor: str = DEFAULT_SUBSCRIPTION_VENDOR
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    transport_conformance: str = "exact"
    reference_profile: str = DEFAULT_REFERENCE_PROFILE
    repair_reference_model: str = "claude4-sonnet"
    trae_capabilities: tuple[str, ...] = ()

    @property
    def uses_trae_workflow(self) -> bool:
        return self.reference_profile != "generic"

    @property
    def requires_exact_transport(self) -> bool:
        return self.uses_trae_workflow and self.transport_conformance == "exact"

    @property
    def exact_trae(self) -> bool:
        """Compatibility alias for full conformance; workflow selection is separate."""
        return self.requires_exact_transport

    @property
    def scheduling_limit(self) -> int:
        if not self.uses_trae_workflow:
            return self.max_iterations
        return {"trae_verified": 51, "trae_multiswe": 101}[self.reference_profile]

    def validate(self) -> None:
        if self.transport_conformance not in {"exact", "adapted"}:
            raise ValueError("transport_conformance must be exact or adapted")
        if self.transport_conformance == "adapted" and (not self.uses_trae_workflow or self.auth != "subscription"):
            raise ValueError("adapted transport requires a Trae profile with subscription auth")
        if not self.repair_reference_model.strip():
            raise ValueError("repair_reference_model must not be empty")
        allowed = {"max_tokens", "n", "temperature", "stop", "reasoning_effort", "tools", "cache_control", "assistant_prefill"}
        if not isinstance(self.trae_capabilities, (tuple, list)) or any(not isinstance(cap, str) or cap not in allowed for cap in self.trae_capabilities):
            raise ValueError("trae_capabilities must be a list of confirmed endpoint capability names")
        if self.requires_exact_transport and self.uses_trae_workflow and self.reasoning_effort not in {None, DEFAULT_REASONING_EFFORT}:
            raise ValueError("reasoning_effort overrides belong to generic mode; exact effort is derived per reference role")
        if self.reference_profile not in {"trae_verified", "trae_multiswe", "generic"}:
            raise ValueError("reference_profile must be trae_verified, trae_multiswe or generic")
        if self.uses_trae_workflow and self.max_iterations != DEFAULT_MAX_ITERATIONS:
            raise ValueError("max_iterations belongs to generic mode; Trae profiles have fixed 50/100 repair turns")
        if not self.model.strip():
            raise ValueError("OpenHands model must not be empty")
        if self.auth not in {"subscription", "api-key"}:
            raise ValueError("OpenHands auth must be 'subscription' or 'api-key'")
        if not self.subscription_vendor.strip():
            raise ValueError("subscription_vendor must not be empty")
        if self.auth == "api-key" and not self.api_key_env.strip():
            raise ValueError("api_key_env must not be empty for api-key auth")
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OpenHandsConfig":
        values = dict(raw)
        unknown = set(values) - {item.name for item in fields(cls)}
        if cls is OpenHandsConfig:
            unknown.discard("max_budget")
        if unknown:
            raise ValueError(f"Unsupported {cls.__name__} fields: {', '.join(sorted(unknown))}")
        if not isinstance(values.get("trae_capabilities", ()), (list, tuple)):
            raise ValueError("trae_capabilities must be a list")
        if values.get("max_budget") is not None:
            raise ValueError("max_budget is no longer supported: tracking tokens only; use max_iterations and timeouts")
        config = cls(
            repair_reference_model=str(values.get("repair_reference_model", "claude4-sonnet")),
            trae_capabilities=tuple(values.get("trae_capabilities", ())),
            transport_conformance=str(values.get("transport_conformance", "exact")),
            reference_profile=str(values.get("reference_profile", DEFAULT_REFERENCE_PROFILE)),
            model=str(values.get("model", DEFAULT_OPENHANDS_MODEL)),
            auth=str(values.get("auth", DEFAULT_OPENHANDS_AUTH)),  # type: ignore[arg-type]
            base_url=values.get("base_url"),
            api_key_env=str(values.get("api_key_env", DEFAULT_API_KEY_ENV)),
            subscription_vendor=str(
                values.get("subscription_vendor", DEFAULT_SUBSCRIPTION_VENDOR)
            ),
            reasoning_effort=values.get("reasoning_effort", DEFAULT_REASONING_EFFORT),
            max_iterations=_as_int(
                values.get("max_iterations", DEFAULT_MAX_ITERATIONS),
                name="max_iterations",
            ),
        )
        config.validate()
        return config

    @classmethod
    def from_env(cls) -> "OpenHandsConfig":
        """Build OpenHands settings from ``AGENTDIET_*`` environment variables."""

        if os.getenv("OPENHANDS_MAX_BUDGET"):
            raise ValueError("OPENHANDS_MAX_BUDGET is no longer supported: tracking tokens only")
        config = cls(
            repair_reference_model=os.getenv("OPENHANDS_REPAIR_REFERENCE_MODEL", "claude4-sonnet"),
            trae_capabilities=tuple(filter(None, (part.strip() for part in os.getenv("OPENHANDS_TRAE_CAPABILITIES", "").split(",")))),
            transport_conformance=os.getenv("OPENHANDS_TRANSPORT_CONFORMANCE", "exact"),
            reference_profile=os.getenv("OPENHANDS_REFERENCE_PROFILE", DEFAULT_REFERENCE_PROFILE),
            # The model is selected explicitly with the CLI --model option.
            model=DEFAULT_OPENHANDS_MODEL,
            auth=os.getenv("OPENHANDS_AUTH", DEFAULT_OPENHANDS_AUTH),  # type: ignore[arg-type]
            base_url=os.getenv("OPENHANDS_BASE_URL") or None,
            api_key_env=os.getenv("OPENHANDS_API_KEY_ENV", DEFAULT_API_KEY_ENV),
            subscription_vendor=os.getenv(
                "OPENHANDS_SUBSCRIPTION_VENDOR", DEFAULT_SUBSCRIPTION_VENDOR
            ),
            reasoning_effort=os.getenv("OPENHANDS_REASONING_EFFORT")
            or DEFAULT_REASONING_EFFORT,
            max_iterations=_as_int(
                os.getenv("OPENHANDS_MAX_ITERATIONS", DEFAULT_MAX_ITERATIONS),
                name="OPENHANDS_MAX_ITERATIONS",
            ),
        )
        config.validate()
        return config


@dataclass(slots=True)
class AgentDietConfig:
    """Trajectory-reduction settings used by ``AgentDietCondenser``."""

    enabled: bool = True
    mode: DietMode = DEFAULT_DIET_MODE
    threshold_tokens: int = DEFAULT_THRESHOLD_TOKENS
    ctx_before: int = DEFAULT_CTX_BEFORE
    ctx_after: int = DEFAULT_CTX_AFTER
    show_ctx: bool = True
    use_lz4: bool = False
    lingua_ratio: float = DEFAULT_LINGUA_RATIO
    compressor_model: str = DEFAULT_COMPRESSOR_MODEL
    compressor_reference_model: str = "gpt-5-mini-2025-08-07"
    compressor_model_explicit: bool = False
    minimum_reduction_tokens: int = DEFAULT_MIN_REDUCTION_TOKENS
    minimum_reduction_ratio: float = DEFAULT_MIN_REDUCTION_RATIO
    keep_raw_events: bool = True

    def validate(self) -> None:
        if self.mode not in {"skip", "ours", "delete", "random", "lingua"}:
            raise ValueError(
                "AgentDiet mode must be one of: skip, ours, delete, random, lingua"
            )
        if self.threshold_tokens < 0:
            raise ValueError("threshold_tokens must be >= 0")
        if self.ctx_before < 0 or self.ctx_after < 0:
            raise ValueError("ctx_before and ctx_after must be >= 0")
        if self.lingua_ratio <= 0 or self.lingua_ratio > 1:
            raise ValueError("lingua_ratio must be in (0, 1]")
        if self.minimum_reduction_tokens < 0:
            raise ValueError("minimum_reduction_tokens must be >= 0")
        if self.minimum_reduction_ratio < 0 or self.minimum_reduction_ratio >= 1:
            raise ValueError("minimum_reduction_ratio must be in [0, 1)")
        if not self.compressor_model.strip():
            raise ValueError("compressor_model must not be empty")
        if not self.compressor_reference_model.strip():
            raise ValueError("compressor_reference_model must not be empty")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AgentDietConfig":
        values = dict(raw)
        unknown = set(values) - {item.name for item in fields(cls)}
        if cls is OpenHandsConfig:
            unknown.discard("max_budget")
        if unknown:
            raise ValueError(f"Unsupported {cls.__name__} fields: {', '.join(sorted(unknown))}")
        config = cls(
            compressor_reference_model=str(values.get("compressor_reference_model", "gpt-5-mini-2025-08-07")),
            compressor_model_explicit=_as_bool(values.get("compressor_model_explicit"), default="compressor_model" in values),
            enabled=_as_bool(values.get("enabled"), default=True),
            mode=str(values.get("mode", DEFAULT_DIET_MODE)),  # type: ignore[arg-type]
            threshold_tokens=_as_int(
                values.get("threshold_tokens", DEFAULT_THRESHOLD_TOKENS),
                name="threshold_tokens",
            ),
            ctx_before=_as_int(
                values.get("ctx_before", DEFAULT_CTX_BEFORE), name="ctx_before"
            ),
            ctx_after=_as_int(
                values.get("ctx_after", DEFAULT_CTX_AFTER), name="ctx_after"
            ),
            show_ctx=_as_bool(values.get("show_ctx"), default=True),
            use_lz4=_as_bool(values.get("use_lz4"), default=False),
            lingua_ratio=_as_float(
                values.get("lingua_ratio", DEFAULT_LINGUA_RATIO), name="lingua_ratio"
            ),
            compressor_model=str(
                values.get("compressor_model", DEFAULT_COMPRESSOR_MODEL)
            ),
            minimum_reduction_tokens=_as_int(
                values.get(
                    "minimum_reduction_tokens", DEFAULT_MIN_REDUCTION_TOKENS
                ),
                name="minimum_reduction_tokens",
            ),
            minimum_reduction_ratio=_as_float(
                values.get(
                    "minimum_reduction_ratio", DEFAULT_MIN_REDUCTION_RATIO
                ),
                name="minimum_reduction_ratio",
            ),
            keep_raw_events=_as_bool(
                values.get("keep_raw_events"), default=True
            ),
        )
        config.validate()
        return config


    @classmethod
    def from_env(cls) -> "AgentDietConfig":
        """Build AgentDiet settings from ``AGENTDIET_*`` environment variables."""

        config = cls(
            compressor_reference_model=os.getenv("AGENTDIET_COMPRESSOR_REFERENCE_MODEL", "gpt-5-mini-2025-08-07"),
            compressor_model_explicit="AGENTDIET_COMPRESSOR_MODEL" in os.environ,
            enabled=_as_bool(
                os.getenv("AGENTDIET_ENABLED"), default=True
            ),
            mode=os.getenv("AGENTDIET_MODE", DEFAULT_DIET_MODE),  # type: ignore[arg-type]
            threshold_tokens=_as_int(
                os.getenv("AGENTDIET_THRESHOLD", DEFAULT_THRESHOLD_TOKENS),
                name="AGENTDIET_THRESHOLD",
            ),
            ctx_before=_as_int(
                os.getenv("AGENTDIET_CTX_BEFORE", DEFAULT_CTX_BEFORE),
                name="AGENTDIET_CTX_BEFORE",
            ),
            ctx_after=_as_int(
                os.getenv("AGENTDIET_CTX_AFTER", DEFAULT_CTX_AFTER),
                name="AGENTDIET_CTX_AFTER",
            ),
            show_ctx=_as_bool(
                os.getenv("AGENTDIET_SHOW_CTX"), default=True
            ),
            use_lz4=_as_bool(
                os.getenv("AGENTDIET_USE_LZ4"), default=False
            ),
            lingua_ratio=_as_float(
                os.getenv("AGENTDIET_LINGUA_RATIO", DEFAULT_LINGUA_RATIO),
                name="AGENTDIET_LINGUA_RATIO",
            ),
            compressor_model=os.getenv(
                "AGENTDIET_COMPRESSOR_MODEL", DEFAULT_COMPRESSOR_MODEL
            ),
            minimum_reduction_tokens=_as_int(
                os.getenv(
                    "AGENTDIET_MIN_REDUCTION_TOKENS",
                    DEFAULT_MIN_REDUCTION_TOKENS,
                ),
                name="AGENTDIET_MIN_REDUCTION_TOKENS",
            ),
            minimum_reduction_ratio=_as_float(
                os.getenv(
                    "AGENTDIET_MIN_REDUCTION_RATIO",
                    DEFAULT_MIN_REDUCTION_RATIO,
                ),
                name="AGENTDIET_MIN_REDUCTION_RATIO",
            ),
            keep_raw_events=_as_bool(
                os.getenv("AGENTDIET_KEEP_RAW_EVENTS"),
                default=True,
            ),
        )
        config.validate()
        return config


@dataclass(slots=True)
class WorkflowConfig:
    """Paths and limits owned by the deterministic repair workflow."""

    input_root: Path | None = None
    output_root: Path | None = DEFAULT_OUTPUT_ROOT
    keep_workspaces: bool = False
    agent_timeout_seconds: int | None = DEFAULT_AGENT_TIMEOUT_SECONDS
    command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS
    validation_timeout_seconds: int = DEFAULT_VALIDATION_TIMEOUT_SECONDS
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES

    def validate(self) -> None:
        for name in ("agent_timeout_seconds", "command_timeout_seconds", "validation_timeout_seconds", "output_limit_bytes"):
            if name == "agent_timeout_seconds" and getattr(self, name) is None:
                continue
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WorkflowConfig":
        values = dict(raw)
        unknown = set(values) - {item.name for item in fields(cls)}
        if cls is OpenHandsConfig:
            unknown.discard("max_budget")
        if unknown:
            raise ValueError(f"Unsupported {cls.__name__} fields: {', '.join(sorted(unknown))}")
        def path(name: str) -> Path | None:
            value = values.get(name)
            return None if value is None else Path(str(value)).expanduser().resolve()
        output_value = values.get("output_root")
        result = cls(
            input_root=path("input_root"),
            output_root=resolve_output_root(output_value),
            keep_workspaces=_as_bool(values.get("keep_workspaces"), default=False),
            agent_timeout_seconds=None if values.get("agent_timeout_seconds") is None else _as_int(values["agent_timeout_seconds"], name="agent_timeout_seconds"),
            command_timeout_seconds=_as_int(values.get("command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS), name="command_timeout_seconds"),
            validation_timeout_seconds=_as_int(values.get("validation_timeout_seconds", DEFAULT_VALIDATION_TIMEOUT_SECONDS), name="validation_timeout_seconds"),
            output_limit_bytes=_as_int(values.get("output_limit_bytes", DEFAULT_OUTPUT_LIMIT_BYTES), name="output_limit_bytes"),
        )
        result.validate()
        return result


@dataclass(slots=True)
class RunConfig:
    """Complete standalone runner configuration."""

    openhands: OpenHandsConfig = field(default_factory=OpenHandsConfig)
    agentdiet: AgentDietConfig = field(default_factory=AgentDietConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    workspace: Path | None = None
    prompt: str | None = None
    prompt_file: Path | None = None
    raw_events_path: Path | None = None
    response_path: Path | None = None

    def validate(self) -> None:
        self.openhands.validate()
        self.agentdiet.validate()
        self.workflow.validate()
        if self.raw_events_path is not None or self.response_path is not None:
            raise ValueError("raw_events_path/response_path overrides are unsupported; use canonical case output paths")
        from .compat.trae_llm_policy import validate_runtime_controls
        validate_runtime_controls(self.openhands, self.workflow)
        if self.openhands.uses_trae_workflow:
            from .compat.trae_diet import validate_exact_diet
            validate_exact_diet(self.agentdiet)
        if self.workspace is not None and not self.workspace.is_absolute():
            raise ValueError("workspace must be an absolute path when provided")
        if self.openhands.uses_trae_workflow and (self.prompt is not None or self.prompt_file is not None):
            raise ValueError("prompt overrides are outside the Trae contract; select reference_profile=generic")
        if self.prompt is not None and self.prompt_file is not None:
            raise ValueError("Set either prompt or prompt_file, not both")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RunConfig":
        unknown = set(raw) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"Unsupported RunConfig fields: {', '.join(sorted(unknown))}")
        openhands_raw = raw.get("openhands", {})
        agentdiet_raw = raw.get("agentdiet", {})
        workflow_raw = raw.get("workflow", {})
        if not isinstance(openhands_raw, Mapping):
            raise ValueError("openhands must be an object")
        if not isinstance(agentdiet_raw, Mapping):
            raise ValueError("agentdiet must be an object")
        if not isinstance(workflow_raw, Mapping):
            raise ValueError("workflow must be an object")

        def path_value(name: str) -> Path | None:
            value = raw.get(name)
            return None if value is None else Path(str(value)).expanduser().resolve()

        config = cls(
            openhands=OpenHandsConfig.from_mapping(openhands_raw),
            agentdiet=AgentDietConfig.from_mapping(agentdiet_raw),
            workflow=WorkflowConfig.from_mapping(workflow_raw),
            workspace=path_value("workspace"),
            prompt=None if raw.get("prompt") is None else str(raw["prompt"]),
            prompt_file=path_value("prompt_file"),
            raw_events_path=path_value("raw_events_path"),
            response_path=path_value("response_path"),
        )
        config.validate()
        return config

    @classmethod
    def from_env(cls) -> "RunConfig":
        config = cls(
            openhands=OpenHandsConfig.from_env(),
            agentdiet=AgentDietConfig.from_env(),
            workspace=(
                Path(os.environ["OPENHANDS_WORKSPACE"]).expanduser().resolve()
                if os.getenv("OPENHANDS_WORKSPACE")
                else None
            ),
            prompt=os.getenv("OPENHANDS_PROMPT") or None,
            prompt_file=(
                Path(os.environ["OPENHANDS_PROMPT_FILE"]).expanduser().resolve()
                if os.getenv("OPENHANDS_PROMPT_FILE")
                else None
            ),
            raw_events_path=(
                Path(os.environ["OPENHANDS_RAW_EVENTS"]).expanduser().resolve()
                if os.getenv("OPENHANDS_RAW_EVENTS")
                else None
            ),
            response_path=(
                Path(os.environ["OPENHANDS_RESPONSE"]).expanduser().resolve()
                if os.getenv("OPENHANDS_RESPONSE")
                else None
            ),
        )
        config.validate()
        return config

    @classmethod
    def load(cls, path: str | Path) -> "RunConfig":
        config_path = Path(path).expanduser().resolve()
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, Mapping):
            raise ValueError(f"Configuration root must be an object: {config_path}")
        return cls.from_mapping(raw)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)

        def serialize(value: Any) -> Any:
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, dict):
                return {key: serialize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [serialize(item) for item in value]
            return value

        return serialize(result)

    def dump(self, path: str | Path) -> None:
        output_path = Path(path).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
