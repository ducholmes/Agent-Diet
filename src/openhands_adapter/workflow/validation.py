"""ContextSniper-style deterministic validation, independent of OpenHands."""
from __future__ import annotations

import re
import subprocess
import time
from types import SimpleNamespace
from dataclasses import replace
from copy import deepcopy
from .validation_reference import ProjectValidator
from .validation_outcome import classify_validation_result
from pathlib import Path

from ..input_loader import CaseSpec, CommandSpec, expand_target_commands
from ..progress import stage
from ..events import emit
from .command import run_command
from .environment import EnvironmentError, ensure_available
from .models import BaselineResult, CommandResult, ValidationResult
from .patch import create_baseline

ZERO_TEST_RE = re.compile(r"(?:no tests? (?:were )?(?:found|ran|to run)|running 0 tests|collected 0 items|ran 0 tests|tests? run:\s*0)", re.I)
EXECUTED_RE = re.compile(r"(?:\b(?:collected|ran)\s+[1-9]\d*\s+(?:items?|tests?)\b|\b[1-9]\d*\s+(?:passed|failed|errors?|tests?)\b|\btest result:\s*(?:ok|failed)\b|^\s*(?:ok|not ok|fail)\s+\S+)", re.I | re.M)


def _output(result: CommandResult) -> str:
    return result.stdout + "\n" + result.stderr


def _command_verdict(spec: CommandSpec, result: CommandResult, *, test: bool) -> tuple[bool, str]:
    """Legacy command diagnostic; final evaluation uses ReferenceExecutor."""
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
        image_id = ensure_available(case.environment, timeout_seconds=timeout_seconds)
        checks["environment"] = "ok"
        if not case.failure_log.read_text(encoding="utf-8").strip():
            raise RuntimeError("external_baseline_output_empty")
        if not case.failing_tests:
            raise RuntimeError("external_baseline_test_ids_empty")
        stage("baseline", "check_passed", check="environment")
        create_baseline(repair, case.failure_log)
        checks["git_baseline"] = "ok"
        tracked = set(subprocess.check_output(('git', '-C', str(repair), 'ls-files', '-z')).decode('utf-8').split('\0'))
        production = [str(path.relative_to(repair)) for path in repair.rglob('*')
                      if path.is_file() and '.git' not in path.relative_to(repair).parts
                      and path.suffix in case.source_extensions]
        untracked = sorted(path for path in production if path not in tracked)
        emit('baseline_source_index', production_paths=sorted(production),
             untracked_production_paths=untracked, tracked_production_count=len(production)-len(untracked))
        if untracked:
            raise RuntimeError('Input production sources are not tracked in baseline; fix case provisioning: ' + ', '.join(untracked))
        checks['tracked_production'] = 'ok'
        stage("baseline", "check_passed", check="git_baseline")
        if not any(validation.iterdir()):
            raise RuntimeError("validation workspace is empty")
        evidence = external_baseline(case)
        evidence["resolved_image_id"] = image_id
        result = BaselineResult(True, checks, test_evidence=evidence)
    except (EnvironmentError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result = BaselineResult(False, checks, str(exc))
    stage("baseline", "checks_finished", result="clean" if result.clean else "failed", elapsed=f"{time.monotonic() - started:.1f}s", reason=result.reason)
    return result


class _ReferenceExecutor:
    """Bridge the pinned evaluator to Agent-Diet's isolated command executor."""

    def __init__(self, case, timeout_seconds, log_dir):
        self.case = case
        self.timeout_seconds = timeout_seconds
        self.log_dir = log_dir
        self.commands = []
        self.snapshots = []
        self.raw_snapshots = []
        self.finished_phases = set()

    def finish_phase(self, phase, evidence):
        if phase in self.finished_phases:
            return
        self.finished_phases.add(phase)
        status = evidence["status"]
        stage("validation", "phase_finished", phase=phase,
              result={"plausible": "passed", "failing": "failed"}.get(status, "invalid"),
              failed_test_ids=(sorted(self._matching_requested_ids(evidence.get("failed_test_ids", []),
                               self.case.failing_tests)) if phase == "target"
                               else evidence.get("failed_test_ids", [])),
              reason=evidence.get("validation_error") or None)

    def _run_plan(self, root, plan, artifact_dir, prefix, test_commands=None, test_scope="regression"):
        if test_scope == "regression" and self.snapshots:
            self.finish_phase("target", self.snapshots[0])
        stage("validation", "phase_started", phase=test_scope)
        return super()._run_plan(root, plan, artifact_dir, prefix,
                                 test_commands=test_commands, test_scope=test_scope)

    def _target_commands(self, plan, failing_tests):
        # Reference expands a placeholder once with an empty selector when the
        # requested set is empty; do not silently drop the configured command.
        selected = self.case if failing_tests else replace(self.case, failing_tests=("",))
        return tuple(_reference_spec(spec, f"target-{index}")
                     for index, (spec, _) in enumerate(_expand_targets(selected), 1))

    def _run_commands(self, root, specs, artifact_dir, prefix, fail_fast=True):
        results = []
        for index, spec in enumerate(specs, 1):
            result = run_command(spec.original, root, timeout_seconds=self.timeout_seconds,
                                 output_limit_bytes=None, environment=self.case.environment,
                                 label=f"{prefix}-{index}", log_dir=self.log_dir)
            result.passed = result.succeeded
            result.failure_reason = None if result.succeeded else (
                "command_timeout" if result.timed_out else f"command_returncode:{result.exit_code}")
            self.commands.append(result)
            record = dict(label=result.label, argv=list(result.argv), cwd=result.cwd,
                          returncode=result.exit_code, timed_out=result.timed_out,
                          output=_output(result), log_path=result.log_path)
            results.append(SimpleNamespace(ok=result.succeeded, output=_output(result),
                           returncode=result.exit_code, timed_out=result.timed_out,
                           label=result.label, as_dict=lambda record=record: record))
            if fail_fast and not result.succeeded:
                break
        return results

    def _snapshot(self, plan, setup, build, tests, status, error,
                  test_case_results=None, test_id_source=""):
        ids = test_case_results or {}
        snapshot = dict(status=status, validation_error=error,
                        tests_executed=bool(tests),
                        failed_test_ids=sorted(k for k, passed in ids.items() if not passed),
                        passed_test_ids=sorted(k for k, passed in ids.items() if passed),
                        test_id_source=test_id_source or "none",
                        setup_commands=[r.as_dict() for r in setup],
                        build_commands=[r.as_dict() for r in build],
                        test_commands=[r.as_dict() for r in tests])
        self.raw_snapshots.append(deepcopy(snapshot))
        self.snapshots.append(snapshot)
        return snapshot


def _reference_spec(spec, label):
    return SimpleNamespace(original=spec, label=label, argv=spec.argv, cwd=spec.cwd,
                           evidence_pattern=spec.evidence_pattern or "",
                           failure_pattern=spec.failure_pattern or "",
                           as_dict=lambda: dict(label=label, argv=list(spec.argv), cwd=spec.cwd,
                                                evidence_pattern=spec.evidence_pattern,
                                                failure_pattern=spec.failure_pattern))


class ReferenceExecutor(_ReferenceExecutor, ProjectValidator):
    pass


def external_baseline(case, failures=None):
    """Freeze caller evidence explicitly, as ContextSniper's benchmark does."""
    ids = ProjectValidator._canonical_test_ids(list(case.failing_tests if failures is None else failures))
    return dict(status="failing" if ids else "plausible", validation_error="",
                failed_test_ids=ids, test_id_source="caller-supplied",
                baseline_external=True, baseline_source="caller-log", baseline_observed=True,
                validation_executed=False, setup_executed=False, compile_executed=False,
                tests_executed=False, baseline_reproduced=False, baseline_executed=False,
                baseline_trust="caller-supplied-log-and-test-ids")


def run_post_patch(case: CaseSpec, workspace: Path, *, timeout_seconds: int,
                   output_limit_bytes: int | None = None, log_dir: Path | None = None,
                   baseline_failures: tuple[str, ...] | None = None) -> ValidationResult:
    """Apply ContextSniper evidence, phase and APR rules to Agent-Diet execution."""
    stage("validation", "started", case=case.case_id, executor="deterministic")
    try:
        ensure_available(case.environment, timeout_seconds=timeout_seconds)
    except EnvironmentError as exc:
        result = ValidationResult(False, "environment", reason=str(exc), status="invalid")
    else:
        executor = ReferenceExecutor(case, timeout_seconds, log_dir)
        plan = SimpleNamespace(system=case.build_system,
               setup=tuple(_reference_spec(s, f"setup-{i}") for i, s in enumerate(case.setup_commands, 1)),
               build=tuple(_reference_spec(s, f"build-{i}") for i, s in enumerate(case.build_commands, 1)),
               regression_test=tuple(_reference_spec(s, f"regression-{i}") for i, s in enumerate(case.regression_test_commands, 1)))
        try:
            snapshot = executor._run_target_and_regression(workspace, plan, log_dir, case.failing_tests)
        except ValueError as exc:
            snapshot = dict(status="invalid", validation_error=str(exc))
        classified = classify_validation_result(external_baseline(case, baseline_failures), snapshot)
        target = executor.snapshots[0] if case.target_test_commands and executor.snapshots else None
        regression = (deepcopy(executor.raw_snapshots[-1]) if executor.snapshots and
                      (not case.target_test_commands or len(executor.snapshots) > 1) else None)
        if regression is not None and str(snapshot.get("validation_error", "")).startswith("regression_"):
            regression.update(status="invalid", validation_error=snapshot["validation_error"])
        target_valid = bool(target and target.get("status") in {"plausible", "failing"}
                            and snapshot.get("target_status") in {"plausible", "failing"}
                            and not str(snapshot.get("validation_error", "")).startswith("target_"))
        regression_valid = bool(regression and regression.get("status") in {"plausible", "failing"}
                                and snapshot.get("status") in {"plausible", "failing"})
        result = ValidationResult(classified["status"] == "plausible", "complete",
                 executor.commands, classified.get("validation_error") or None,
                 status=classified["status"], target_valid=target_valid,
                 regression_valid=regression_valid, environment_ready=True,
                 target_failures=tuple(sorted(executor._matching_requested_ids(
                     target.get("failed_test_ids", []), case.failing_tests))) if target else (),
                 regression_failures=tuple(regression.get("failed_test_ids", [])) if regression else (),
                 failed_test_ids=tuple(classified["post_failed_test_ids"]),
                 fixed_test_ids=tuple(classified["fixed_test_ids"]),
                 regression_test_ids=tuple(classified["regression_test_ids"]),
                 evidence=classified)
        for phase, evidence in (("target", target), ("regression", regression)):
            if evidence is not None:
                executor.finish_phase(phase, evidence)
    stage("validation", "finished", case=case.case_id, phase=result.phase,
          result=result.status, passed=result.passed, reason=result.reason)
    return result
