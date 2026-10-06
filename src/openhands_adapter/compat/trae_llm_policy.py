"""Role policies from the frozen Trae caller and OpenAI wrapper.

Reference names choose protocol branches; actual model IDs never do.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal
import time


@dataclass(frozen=True, slots=True)
class TraeLLMPolicy:
    role: Literal["repair", "compression"]
    reference_model: str

    def __post_init__(self):
        if self.role not in {"repair", "compression"} or not self.reference_model.strip():
            raise ValueError("Trae policy requires a role and nonempty reference model")
        # The source OpenAI wrapper calls model_dump on the completion, and
        # cannot drain a Qwen streaming iterator. Do not silently ignore stream.
        if self.role == "compression" and "qwen3-235b-a22b-instruct-2507" in self.reference_model:
            raise ValueError("compression: Qwen streaming reference is not supported by the frozen OpenAI wrapper")

    @property
    def bypass_filter(self) -> bool:
        return "gpt-5-" in self.reference_model

    def params(self) -> dict:
        result = {"max_tokens": 8192, "n": 1, "temperature": 0.0}
        if self.role == "compression":
            result["stop"] = "</step>"
        if self.bypass_filter:
            result.pop("temperature")
            result.pop("stop", None)
            result["reasoning_effort"] = "low"
        return result

    @property
    def required_capabilities(self) -> frozenset[str]:
        return frozenset({*self.params(), "cache_control",
                          "assistant_prefill" if self.role == "compression" else "tools"})

    def validate_transport(self, *, subscription: bool, capabilities=()) -> None:
        if subscription:
            raise ValueError(f"{self.role}: subscription/Responses cannot preserve the Trae "
                             "cap, sampling, cache and assistant continuation contract; "
                             "use a compatible api-key Chat Completions endpoint or reference_profile=generic")
        missing = self.required_capabilities - set(capabilities)
        if missing:
            raise ValueError(f"{self.role}: endpoint capabilities not confirmed: {', '.join(sorted(missing))}; "
                             "configure trae_capabilities from the endpoint contract or use reference_profile=generic")


def send_with_reference_retry(one_attempt: Callable, sleep: Callable = time.sleep, *, on_error: Callable | None = None):
    """Twelve total attempts, including the source's sleep after final failure.

    The attempt owns provider response normalization, but no downstream parsing
    or telemetry. No completed-response cache exists here.
    """
    for retries in range(12):
        try:
            completion = one_attempt()
            if completion is None:
                raise RuntimeError("completion is None")
            return completion
        except Exception as exc:
            if on_error is not None:
                on_error(exc, retries + 1, 2 ** retries)
            sleep(2 ** retries)
    return None


def validate_runtime_controls(openhands, workflow):
    if workflow is not None:
        workflow.validate()


def validate_trae_run(openhands, diet, workflow=None):
    """Preflight before login, container creation or the first repair call."""
    openhands.validate()
    diet.validate()
    validate_runtime_controls(openhands, workflow)
    if not openhands.uses_trae_workflow:
        return None, None
    import os
    if not os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock').startswith('unix://'):
        raise ValueError('exact profile requires a local Unix Docker endpoint for source exec semantics')
    from .trae_diet import validate_exact_diet
    validate_exact_diet(diet)
    repair = TraeLLMPolicy("repair", openhands.repair_reference_model)
    if openhands.requires_exact_transport:
        repair.validate_transport(subscription=openhands.auth == "subscription", capabilities=openhands.trae_capabilities)
    compression = None
    if diet.enabled and diet.mode == "ours":
        if diet.compressor_model == "inherit" and not diet.compressor_model_explicit:
            raise ValueError("compression: declare compressor_model explicitly (including deliberate 'inherit') for exact mode")
        compression = TraeLLMPolicy("compression", diet.compressor_reference_model)
        if openhands.requires_exact_transport:
            compression.validate_transport(subscription=openhands.auth == "subscription", capabilities=openhands.trae_capabilities)
    return repair, compression
