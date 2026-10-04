"""ContextSniper-style deterministic validation, independent of OpenHands."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from ..input_loader import CaseSpec, CommandSpec, expand_target_commands
from ..progress import stage
from .command import run_command
from .environment import EnvironmentError, ensure_available
from .models import BaselineResult, CommandResult, ValidationResult
from .outcome import classify
from .patch import create_baseline

ZERO_TEST_RE = re.compile(r"(?:no tests? (?:were )?(?:found|ran|to run)|running 0 tests|collected 0 items|ran 0 tests|tests? run:\s*0)", re.I)
EXECUTED_RE = re.compile(r"(?:\b(?:collected|ran)\s+[1-9]\d*\s+(?:items?|tests?)\b|\b[1-9]\d*\s+(?:passed|failed|errors?|tests?)\b|\btest result:\s*(?:ok|failed)\b|^\s*(?:ok|not ok|fail)\s+\S+)", re.I | re.M)


def _output(result: CommandResult) -> str:
    return result.stdout + "\n" + result.stderr


def _command_verdict(spec: CommandSpec, result: CommandResult, *, test: bool) -> tuple[bool, str]:
    output = _output(result)
    if result.timed_out:
        return False, "command_timeout"
    if result.exit_code != 0:
        return False, f"command_returncode:{result.exit_code}"
    if not test:
        return True, ""
    if ZERO_TEST_RE.search(output):
        return False, "no_tests_discovered"
    if "command not found" in output.lower():
        return False, "test_runner_unavailable"
    if spec.evidence_pattern and not re.search(spec.evidence_pattern, output, re.M):
        return False, "evidence_pattern_not_matched"
    if spec.failure_pattern and re.search(spec.failure_pattern, output, re.M):
        return False, "failure_pattern_matched"
    if not spec.evidence_pattern and not spec.failure_pattern and not EXECUTED_RE.search(output):
        return False, "test_execution_unverified"
    return True, ""


def _execution_valid(spec: CommandSpec, result: CommandResult) -> bool:
    """A failing test is valid only when output proves that it actually ran."""
    output = _output(result)
    if result.timed_out or not output.strip() or ZERO_TEST_RE.search(output):
        return False
    if result.exit_code in {124, 125, 126, 127} or "command not found" in output.lower():
        return False
    if spec.evidence_pattern or spec.failure_pattern:
        return bool(
            (spec.evidence_pattern and re.search(spec.evidence_pattern, output, re.M))
            or (spec.failure_pattern and re.search(spec.failure_pattern, output, re.M))
        )
    return bool(EXECUTED_RE.search(output))


def _regression_ids(result: CommandResult, index: int) -> set[str]:
    """Keep parsed regression IDs when possible, with an auditable fallback."""
    found: set[str] = set()
    for pattern in (r"^\*{3}\s+\[err\]:\s+(.+?)\s+in\s+tests/", r"^\[err\]:\s+(.+?)\s+in\s+tests/", r"^FAILED\s+(.+?)\s*$"):
        for match in re.finditer(pattern, _output(result), re.M):
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            if value:
                found.add(f"__regression__:{value}")
    return found or {f"__regression__:{index}"}


def _expand_targets(case: CaseSpec) -> tuple[tuple[CommandSpec, str | None], ...]:
    return expand_target_commands(case)


def run_baseline(case: CaseSpec, repair: Path, validation: Path, *, timeout_seconds: int = 60) -> BaselineResult:
    checks: dict[str, str] = {}
    started = time.monotonic()
    stage("baseline", "checks_started", case=case.case_id)
    try:
        for name, path in (("source", case.source_project), ("config", case.config_path), ("failure_log", case.failure_log)):
            if not path.exists():
                raise RuntimeError(f"missing {name}")
            checks[name] = "ok"
            stage("baseline", "check_passed", check=name)
        ensure_available(case.environment, timeout_seconds=timeout_seconds)
        checks["environment"] = "ok"
        stage("baseline", "check_passed", check="environment")
        create_baseline(repair, case.failure_log)
        checks["git_baseline"] = "ok"
        stage("baseline", "check_passed", check="git_baseline")
        if not any(validation.iterdir()):
            raise RuntimeError("validation workspace is empty")
        result = BaselineResult(True, checks)
    except (EnvironmentError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result = BaselineResult(False, checks, str(exc))
    stage("baseline", "checks_finished", result="clean" if result.clean else "failed", elapsed=f"{time.monotonic() - started:.1f}s", reason=result.reason)
    return result


def _run_phase(case: CaseSpec, workspace: Path, phase: str, specs: tuple[CommandSpec, ...], *, timeout_seconds: int, output_limit_bytes: int | None, log_dir: Path | None) -> tuple[list[CommandResult], str | None]:
    results: list[CommandResult] = []
    stage("validation", "phase_started", phase=phase, commands=len(specs))
    for index, spec in enumerate(specs, 1):
        result = run_command(spec, workspace, timeout_seconds=timeout_seconds, output_limit_bytes=output_limit_bytes, environment=case.environment, label=f"{phase}-{index}", log_dir=log_dir)
        passed, reason = _command_verdict(spec, result, test=False)
        result.passed, result.failure_reason = passed, reason or None
        results.append(result)
        if not passed:
            stage("validation", "phase_failed", phase=phase, reason=reason)
            return results, f"{phase}_failed:{reason}"
    stage("validation", "phase_finished", phase=phase, result="passed")
    return results, None


def run_post_patch(case: CaseSpec, workspace: Path, *, timeout_seconds: int, output_limit_bytes: int | None = None, log_dir: Path | None = None, baseline_failures: tuple[str, ...] | None = None) -> ValidationResult:
    """Run setup/build, all targets, and regression; never trust model output."""
    commands: list[CommandResult] = []
    started = time.monotonic()
    stage("validation", "started", case=case.case_id, executor="deterministic")

    def finish(result: ValidationResult) -> ValidationResult:
        stage(
            "validation",
            "finished",
            case=case.case_id,
            phase=result.phase,
            result=result.status,
            passed=result.passed,
            commands=len(result.commands),
            elapsed=f"{time.monotonic() - started:.1f}s",
            reason=result.reason,
        )
        return result

    try:
        ensure_available(case.environment, timeout_seconds=timeout_seconds)
    except EnvironmentError as exc:
        return finish(ValidationResult(False, "environment", commands, str(exc), status="invalid"))
    for phase, specs in (("setup", case.setup_commands), ("build", case.build_commands)):
        phase_commands, error = _run_phase(case, workspace, phase, specs, timeout_seconds=timeout_seconds, output_limit_bytes=output_limit_bytes, log_dir=log_dir)
        commands.extend(phase_commands)
        if error:
            return finish(ValidationResult(False, phase, commands, error, status="invalid", environment_ready=True))

    try:
        targets = _expand_targets(case)
    except ValueError as exc:
        return finish(ValidationResult(False, "target", commands, str(exc), status="invalid", environment_ready=True))
    if not targets:
        return finish(ValidationResult(False, "target", commands, "target_test_empty", status="invalid", environment_ready=True))
    stage("validation", "phase_started", phase="target", commands=len(targets))
    target_failures: set[str] = set()
    target_valid = True
    for index, (spec, test_id) in enumerate(targets, 1):
        result = run_command(spec, workspace, timeout_seconds=timeout_seconds, output_limit_bytes=output_limit_bytes, environment=case.environment, label=f"target-{index}" + (f"-{test_id}" if test_id else ""), log_dir=log_dir)
        passed, reason = _command_verdict(spec, result, test=True)
        result.passed, result.failure_reason = passed, reason or None
        commands.append(result)
        if not _execution_valid(spec, result):
            target_valid = False
        if not passed:
            target_failures.update((test_id,) if test_id else case.failing_tests)
    if not target_valid:
        return finish(ValidationResult(False, "target", commands, "target_test_invalid", status="invalid", target_failures=tuple(sorted(target_failures)), target_valid=False, environment_ready=True))
    stage("validation", "phase_finished", phase="target", result="passed")

    if not case.regression_test_commands:
        return finish(ValidationResult(False, "regression", commands, "regression_test_empty", status="invalid", target_failures=tuple(sorted(target_failures)), target_valid=True, environment_ready=True))
    stage("validation", "phase_started", phase="regression", commands=len(case.regression_test_commands))
    regression_failures: set[str] = set()
    regression_valid = True
    for index, spec in enumerate(case.regression_test_commands, 1):
        if any("{test_id}" in arg for arg in spec.argv):
            return finish(ValidationResult(False, "regression", commands, "regression_test_contains_test_id", status="invalid", target_failures=tuple(sorted(target_failures)), target_valid=True, environment_ready=True))
        result = run_command(spec, workspace, timeout_seconds=timeout_seconds, output_limit_bytes=output_limit_bytes, environment=case.environment, label=f"regression-{index}", log_dir=log_dir)
        passed, reason = _command_verdict(spec, result, test=True)
        result.passed, result.failure_reason = passed, reason or None
        commands.append(result)
        if not _execution_valid(spec, result):
            regression_valid = False
        if not passed:
            regression_failures.update(_regression_ids(result, index))
    post = tuple(sorted(target_failures | regression_failures))
    outcome = classify(baseline_failures or case.failing_tests, post, valid=target_valid and regression_valid)
    stage("validation", "phase_finished", phase="regression", result=outcome.status)
    return finish(ValidationResult(outcome.status == "plausible", "complete", commands, None if outcome.status != "invalid" else "validation_invalid", status=outcome.status, target_failures=tuple(sorted(target_failures)), regression_failures=tuple(sorted(regression_failures)), failed_test_ids=post, fixed_test_ids=outcome.fixed_test_ids, regression_test_ids=outcome.regression_test_ids, target_valid=target_valid, regression_valid=regression_valid, environment_ready=True))
