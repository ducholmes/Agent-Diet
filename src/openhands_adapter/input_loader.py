"""Discover and validate prepared repair cases.

A prepared case uses three sibling paths under an input root::

    inputs/<case-id>/
    inputs/<case-id>.debugging-framework.json
    inputs/<case-id>.failure.log

Or the directory layout used by SWE-bench prepared cases::

    inputs/<case-id>/config.json
    inputs/<case-id>/failure.log
    inputs/<case-id>/<case-id>/

This module only reads and validates metadata.  It deliberately does not copy
workspaces, inspect Docker, or execute setup/build/test commands.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


CONFIG_SUFFIX = ".debugging-framework.json"
FAILURE_SUFFIX = ".failure.log"
SUPPORTED_ENVIRONMENT_MODE = "image"
SUPPORTED_CONTAINER_RUNTIME = "docker"


class InputLoadError(ValueError):
    """A prepared input cannot be discovered or parsed safely."""


class CaseNotFoundError(InputLoadError):
    """No prepared case matches the requested selector."""


class AmbiguousCaseError(InputLoadError):
    """A selector matches more than one prepared case."""


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One normalized command from a Debugging Framework phase."""

    argv: tuple[str, ...]
    cwd: str = "."
    evidence_pattern: str | None = None
    failure_pattern: str | None = None


@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    """Container environment required to run a prepared case."""

    mode: str
    runtime: str
    image: str


@dataclass(frozen=True, slots=True)
class CaseSpec:
    """Validated, execution-independent description of one repair case."""

    case_id: str
    relative_id: str
    input_root: Path
    source_project: Path
    config_path: Path
    failure_log: Path
    schema_version: int | None
    environment: EnvironmentSpec
    setup_commands: tuple[CommandSpec, ...]
    build_commands: tuple[CommandSpec, ...]
    target_test_commands: tuple[CommandSpec, ...]
    regression_test_commands: tuple[CommandSpec, ...]
    failing_tests: tuple[str, ...]
    source_extensions: tuple[str, ...]
    workspace_disposable: bool
    initialize_git_if_missing: bool
    problem_statement: str | None = None
    issue_source: str = "failure_log"
    issue_encoding: str = "utf-8"
    issue_sha256: str = ""
    build_system: str = "custom"


@dataclass(frozen=True, slots=True)
class _CasePaths:
    case_id: str
    relative_id: str
    project: Path
    config: Path
    failure: Path


def expand_target_commands(case: CaseSpec) -> tuple[tuple[CommandSpec, str | None], ...]:
    """Resolve targets identically for the repair agent and evaluator."""
    invocations: list[tuple[CommandSpec, str | None]] = []
    for spec in case.target_test_commands:
        if any("{test_id}" in arg for arg in spec.argv):
            for test_id in case.failing_tests:
                if any(char in test_id for char in ("\0", "\r", "\n")):
                    raise ValueError("test ID contains a forbidden control character")
                invocations.append((replace(spec, argv=tuple(
                    arg.replace("{test_id}", test_id) for arg in spec.argv
                )), test_id))
        else:
            invocations.append((spec, None))
    return tuple(invocations)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputLoadError(f"Unable to read case config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputLoadError(f"Invalid JSON in case config {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise InputLoadError(f"Case config root must be an object: {path}")
    return raw


def _case_id_from_config(path: Path) -> str:
    if not path.name.endswith(CONFIG_SUFFIX):
        raise InputLoadError(f"Unexpected case config filename: {path}")
    case_id = path.name[: -len(CONFIG_SUFFIX)]
    if not case_id or case_id in {".", ".."}:
        raise InputLoadError(f"Invalid case id derived from config: {path}")
    return case_id


def _discover_case_paths(input_root: str | Path) -> tuple[_CasePaths, ...]:
    root = Path(input_root).expanduser().resolve()
    if not root.is_dir():
        raise InputLoadError(f"Input root is not a directory: {root}")

    cases: list[_CasePaths] = []
    incomplete: list[str] = []
    candidates: list[tuple[Path, str, Path, Path, str]] = []
    for config_path in sorted(root.rglob(f"*{CONFIG_SUFFIX}")):
        case_id = _case_id_from_config(config_path)
        project = config_path.parent / case_id
        failure = config_path.parent / f"{case_id}{FAILURE_SUFFIX}"
        relative_parent = config_path.parent.relative_to(root)
        relative_id = (relative_parent / case_id).as_posix()
        candidates.append((config_path, case_id, project, failure, relative_id))

    for config_path in sorted(root.rglob("config.json")):
        parent = config_path.parent
        failure = parent / "failure.log"
        # Ignore unrelated config.json files in the source checkout.
        if not failure.is_file() and not (parent / parent.name).is_dir():
            continue
        if not _is_relative_to(config_path.resolve(), root):
            raise InputLoadError(f"Prepared case config {config_path} resolves outside input root {root}")
        config = _read_json_object(config_path)
        case_id = config.get("project_id", parent.name)
        if (not isinstance(case_id, str) or not case_id.strip()
                or case_id in {".", ".."} or "/" in case_id or "\\" in case_id):
            raise InputLoadError(f"Invalid config.project_id in {config_path}")
        project = parent / case_id
        relative_parent = parent.relative_to(root)
        relative_id = case_id if relative_parent == Path(".") else relative_parent.as_posix()
        candidates.append((config_path, case_id, project, failure, relative_id))

    seen_projects: set[Path] = set()
    for config_path, case_id, project, failure, relative_id in candidates:
        if not project.is_dir() or not failure.is_file():
            missing = []
            if not project.is_dir():
                missing.append(str(project))
            if not failure.is_file():
                missing.append(str(failure))
            incomplete.append(f"{case_id}: missing {', '.join(missing)}")
            continue

        resolved_config = config_path.resolve()
        resolved_project = project.resolve()
        resolved_failure = failure.resolve()
        if not all(
            _is_relative_to(path, root)
            for path in (resolved_config, resolved_project, resolved_failure)
        ):
            raise InputLoadError(
                f"Prepared case {case_id!r} resolves outside input root {root}"
            )

        # A metadata alias beside config.json must not run the same source twice.
        if resolved_project in seen_projects:
            continue
        seen_projects.add(resolved_project)
        cases.append(
            _CasePaths(
                case_id=case_id,
                relative_id=relative_id,
                project=resolved_project,
                config=resolved_config,
                failure=resolved_failure,
            )
        )

    if not cases:
        detail = f" ({'; '.join(incomplete)})" if incomplete else ""
        raise CaseNotFoundError(f"No complete prepared cases found under {root}{detail}")
    return tuple(sorted(cases, key=lambda case: case.relative_id))


def _normalize_cwd(raw: Any, *, context: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise InputLoadError(f"{context}.cwd must be a non-empty string")
    cwd = PurePosixPath(raw.strip())
    if cwd.is_absolute() or ".." in cwd.parts:
        raise InputLoadError(
            f"{context}.cwd must stay inside the project, got {raw!r}"
        )
    normalized = cwd.as_posix()
    return "." if normalized in {"", "."} else normalized


def _normalize_argv(raw: Any, *, context: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        try:
            argv = tuple(shlex.split(raw))
        except ValueError as exc:
            raise InputLoadError(f"Invalid shell quoting in {context}: {exc}") from exc
    elif isinstance(raw, Sequence) and not isinstance(
        raw, (str, bytes, bytearray)
    ):
        values: list[str] = []
        for index, value in enumerate(raw):
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise InputLoadError(
                    f"{context}.command[{index}] must be a string or number"
                )
            values.append(str(value))
        argv = tuple(values)
    else:
        raise InputLoadError(
            f"{context}.command must be a command string or argument list"
        )

    if not argv or not argv[0].strip():
        raise InputLoadError(f"{context}.command must not be empty")
    return argv


def _optional_pattern(raw: Any, *, name: str, context: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise InputLoadError(f"{context}.{name} must be a non-empty string")
    try:
        re.compile(raw)
    except re.error as exc:
        raise InputLoadError(f"Invalid {context}.{name}: {exc}") from exc
    return raw


def parse_command_spec(raw: Any, *, context: str = "command") -> CommandSpec:
    """Normalize a string, argument list, or command object."""

    if isinstance(raw, Mapping):
        if "command" not in raw:
            raise InputLoadError(f"{context} object is missing 'command'")
        command = raw["command"]
        cwd = _normalize_cwd(raw.get("cwd", "."), context=context)
        evidence_pattern = _optional_pattern(
            raw.get("evidence_pattern"), name="evidence_pattern", context=context
        )
        failure_pattern = _optional_pattern(
            raw.get("failure_pattern"), name="failure_pattern", context=context
        )
    else:
        command = raw
        cwd = "."
        evidence_pattern = None
        failure_pattern = None

    return CommandSpec(
        argv=_normalize_argv(command, context=context),
        cwd=cwd,
        evidence_pattern=evidence_pattern,
        failure_pattern=failure_pattern,
    )


def _phase_specs(config: Mapping[str, Any], phase: str) -> tuple[CommandSpec, ...]:
    raw = config.get(phase)
    if phase == "regression_test" and raw in (None, []):
        raw = config.get("test")
    if raw in (None, []):
        return ()

    # A scalar string/dict is one command. A flat scalar list is one argv list.
    # A nested list is a list of command specs.
    if isinstance(raw, (str, Mapping)):
        specs = [raw]
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        if raw and all(
            not isinstance(value, (Mapping, Sequence))
            or isinstance(value, (str, bytes, bytearray))
            for value in raw
        ):
            specs = [raw]
        else:
            specs = list(raw)
    else:
        raise InputLoadError(
            f"config.{phase} must be a command or a list of commands"
        )

    return tuple(
        parse_command_spec(spec, context=f"config.{phase}[{index}]")
        for index, spec in enumerate(specs)
    )


def _string_tuple(raw: Any, *, context: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InputLoadError(f"{context} must be a list of strings")
    values: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            raise InputLoadError(f"{context}[{index}] must be a non-empty string")
        values.append(value.strip())
    return tuple(values)


def _environment_spec(config: Mapping[str, Any]) -> EnvironmentSpec:
    raw = config.get("environment")
    if not isinstance(raw, Mapping):
        raise InputLoadError("config.environment must be an object")

    mode = str(raw.get("mode", SUPPORTED_ENVIRONMENT_MODE)).strip().lower()
    runtime = str(raw.get("runtime", SUPPORTED_CONTAINER_RUNTIME)).strip().lower()
    image = str(raw.get("image", "")).strip()
    if mode != SUPPORTED_ENVIRONMENT_MODE:
        raise InputLoadError(
            f"Unsupported config.environment.mode {mode!r}; MVP requires 'image'"
        )
    if runtime != SUPPORTED_CONTAINER_RUNTIME:
        raise InputLoadError(
            f"Unsupported config.environment.runtime {runtime!r}; MVP requires 'docker'"
        )
    if not image:
        raise InputLoadError("config.environment.image must not be empty")
    return EnvironmentSpec(mode=mode, runtime=runtime, image=image)


def _optional_schema_version(config: Mapping[str, Any]) -> int | None:
    raw = config.get("schema_version")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise InputLoadError("config.schema_version must be a positive integer")
    return raw


def _bool_value(raw: Any, *, context: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise InputLoadError(f"{context} must be a boolean")
    return raw


def _load_case_paths(paths: _CasePaths, root: Path) -> CaseSpec:
    config = _read_json_object(paths.config)
    repair = config.get("repair", {})
    workspace = config.get("workspace", {})
    if not isinstance(repair, Mapping):
        raise InputLoadError("config.repair must be an object")
    if not isinstance(workspace, Mapping):
        raise InputLoadError("config.workspace must be an object")

    import hashlib
    issue = config.get("problem_statement", repair.get("problem_statement"))
    source = "problem_statement"
    if issue is None:
        source = "failure_log"
        try:
            issue = paths.failure.read_bytes().decode("utf-8")
        except UnicodeError as exc:
            raise InputLoadError("failure log must be valid UTF-8") from exc
    if not isinstance(issue, str) or not issue.strip():
        raise InputLoadError("problem_statement/failure log must contain nonempty issue text")
    return CaseSpec(
        problem_statement=issue,
        issue_source=source,
        issue_encoding="utf-8",
        issue_sha256=hashlib.sha256(issue.encode("utf-8")).hexdigest(),
        case_id=paths.case_id,
        relative_id=paths.relative_id,
        input_root=root,
        source_project=paths.project,
        config_path=paths.config,
        failure_log=paths.failure,
        schema_version=_optional_schema_version(config),
        build_system=str(config.get("build_system", config.get("system", "custom"))),
        environment=_environment_spec(config),
        setup_commands=_phase_specs(config, "setup"),
        build_commands=_phase_specs(config, "build"),
        target_test_commands=_phase_specs(config, "target_test"),
        regression_test_commands=_phase_specs(config, "regression_test"),
        failing_tests=_string_tuple(
            repair.get("failing_tests"), context="config.repair.failing_tests"
        ),
        source_extensions=_string_tuple(
            repair.get("source_extensions"), context="config.repair.source_extensions"
        ),
        workspace_disposable=_bool_value(
            workspace.get("disposable"),
            context="config.workspace.disposable",
            default=True,
        ),
        initialize_git_if_missing=_bool_value(
            workspace.get("initialize_git_if_missing"),
            context="config.workspace.initialize_git_if_missing",
            default=True,
        ),
    )


def discover_cases(input_root: str | Path) -> tuple[CaseSpec, ...]:
    """Discover and fully validate every complete case below ``input_root``."""

    root = Path(input_root).expanduser().resolve()
    return tuple(_load_case_paths(paths, root) for paths in _discover_case_paths(root))


def load_case(input_root: str | Path, selector: str | None = None) -> CaseSpec:
    """Load one case by exact case ID/relative ID or an unambiguous prefix.

    When ``selector`` is omitted the input root must contain exactly one complete
    case. Exact matches take precedence over prefix matches.
    """

    root = Path(input_root).expanduser().resolve()
    cases = _discover_case_paths(root)
    if selector is None:
        matches = list(cases)
    else:
        requested = selector.strip().strip("/")
        if not requested:
            raise CaseNotFoundError("Case selector must not be empty")
        exact = [
            case
            for case in cases
            if requested in {case.case_id, case.relative_id}
        ]
        matches = exact or [
            case
            for case in cases
            if case.case_id.startswith(requested)
            or case.relative_id.startswith(requested)
        ]

    if not matches:
        available = ", ".join(case.relative_id for case in cases)
        raise CaseNotFoundError(
            f"No prepared case matches {selector!r}; available: {available}"
        )
    if len(matches) != 1:
        matched = ", ".join(case.relative_id for case in matches)
        label = selector if selector is not None else "<unspecified>"
        raise AmbiguousCaseError(
            f"Case selector {label!r} is ambiguous; matches: {matched}"
        )
    return _load_case_paths(matches[0], root)


# Descriptive alias used by the workflow runner.
load_input = load_case


__all__ = [
    "AmbiguousCaseError",
    "CaseNotFoundError",
    "CaseSpec",
    "CommandSpec",
    "CONFIG_SUFFIX",
    "EnvironmentSpec",
    "FAILURE_SUFFIX",
    "InputLoadError",
    "discover_cases",
    "load_case",
    "load_input",
    "parse_command_spec",
]
