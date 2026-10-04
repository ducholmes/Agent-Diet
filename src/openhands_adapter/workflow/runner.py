"""One-case orchestrator. Validation is isolated from agent implementation."""
from __future__ import annotations
from collections.abc import Callable
from pathlib import Path
import time
from ..config import DEFAULT_OUTPUT_ROOT, WorkflowConfig
from ..events import configure, emit, latest, read_events
from ..token_tracking import aggregate_calls, summarize_diet
from ..input_loader import CaseSpec, load_case
from ..progress import stage
from .artifacts import write_json, write_text
from .models import BaselineResult, RunResult, ValidationResult
from .patch import PatchError, apply_patch, assert_safe_patch, capture_patch
from .validation import run_baseline, run_post_patch
from .workspace import create_workspaces

AgentRunner = Callable[[Path, Path], str]


def _baseline_summary(result: BaselineResult) -> dict:
    return {
        "clean": result.clean,
        "checks": result.checks,
        "reason": result.reason,
    }


def _validation_summary(result: ValidationResult) -> dict:
    return {
        "passed": result.passed,
        "status": result.status,
        "phase": result.phase,
        "reason": result.reason,
        "target_valid": result.target_valid,
        "regression_valid": result.regression_valid,
        "environment_ready": result.environment_ready,
        "target_failures": list(result.target_failures),
        "regression_failures": list(result.regression_failures),
        "failed_test_ids": list(result.failed_test_ids),
        "fixed_test_ids": list(result.fixed_test_ids),
        "regression_test_ids": list(result.regression_test_ids),
        "commands": [
            {
                "label": command.label,
                "argv": list(command.argv),
                "cwd": command.cwd,
                "exit_code": command.exit_code,
                "timed_out": command.timed_out,
                "elapsed_seconds": round(command.elapsed_seconds, 3),
                "passed": command.passed,
                "failure_reason": command.failure_reason,
                "log_path": command.log_path,
            }
            for command in result.commands
        ],
    }


def _error(phase: str, message: str, exc: BaseException | None = None) -> dict:
    value = {"phase": phase, "message": message}
    if exc is not None:
        value["type"] = type(exc).__name__
    return value


def _agent_summary(events_path: Path, status: str, elapsed: float, error: dict | None) -> dict:
    summary = {
        "status": status,
        "elapsed_seconds": round(elapsed, 3),
        "error": error,
    }
    process_event = latest(events_path, "agent_process")
    if process_event:
        summary.update(
            returncode=process_event.get("returncode"),
            timed_out=process_event.get("timed_out"),
            worker_elapsed_seconds=process_event.get("elapsed_seconds"),
        )
    return summary


def run_case(case: CaseSpec | Path | str, workflow: WorkflowConfig, *, selector: str | None = None, agent_runner: AgentRunner | None = None) -> RunResult:
    if not isinstance(case, CaseSpec): case = load_case(case, selector)
    workflow.validate()
    output = (workflow.output_root or DEFAULT_OUTPUT_ROOT).resolve() / case.relative_id; output.mkdir(parents=True, exist_ok=True)
    events_path = output / "events.jsonl"
    configure(events_path, reset=True)
    # A rerun replaces this case's artifacts. Clear prior results too, so an
    # early failure cannot leave a previous patch or response beside new events.
    for obsolete in (
        output / "baseline.json", output / "validation.json", output / "diet-events.jsonl",
        output / "result.json", output / "patch.diff", output / "response.txt",
    ):
        obsolete.unlink(missing_ok=True)
    (output / "logs" / "worker-config.json").unlink(missing_ok=True)
    started = time.monotonic()
    timings: dict[str, float] = {}
    emit("run_started", case_id=case.case_id)
    workspace_parent = output / ("workspaces" if workflow.keep_workspaces else ".tmp")
    workspace_parent.mkdir(parents=True, exist_ok=True)
    spaces = None

    def finish(result: RunResult) -> RunResult:
        result.elapsed_seconds = round(time.monotonic() - started, 3)
        timings["total"] = result.elapsed_seconds
        result.timings = {key: round(value, 3) for key, value in timings.items()}
        events = read_events(events_path)
        result.token_usage = aggregate_calls(events)
        result.diet_metrics = summarize_diet(events, result.token_usage)
        emit(
            "run_finished",
            case_id=case.case_id,
            resolved=result.resolved,
            validation=result.validation,
            elapsed_seconds=result.elapsed_seconds,
            token_usage=result.token_usage,
            error=result.error,
        )
        write_json(output / "result.json", result)
        return result

    try:
        spaces = create_workspaces(
            case.source_project,
            parent=workspace_parent,
            keep=workflow.keep_workspaces,
        )
        stage("baseline", "starting", case=case.case_id)
        baseline_started = time.monotonic()
        baseline = run_baseline(case, spaces.repair, spaces.validation, timeout_seconds=workflow.validation_timeout_seconds)
        timings["baseline"] = time.monotonic() - baseline_started
        stage("baseline", "finished", case=case.case_id, result="clean" if baseline.clean else "failed", elapsed=f"{timings['baseline']:.1f}s", reason=baseline.reason)
        if not baseline.clean:
            result = RunResult(
                case.case_id, "failed", "not_started", False, False, "not_run", False,
                "baseline_failed", baseline_summary=_baseline_summary(baseline),
                error=_error("baseline", baseline.reason or "baseline_failed"),
            )
            return finish(result)
        agent_status = "completed"; response = ""; agent_error: dict | None = None
        agent_started = time.monotonic()
        if agent_runner is not None:
            stage("repair", "started", case=case.case_id, agent="OpenHands")
            try: response = agent_runner(spaces.repair, output)
            except TimeoutError as exc:
                agent_status, response = "timeout", ""
                agent_error = _error("agent", str(exc) or "OpenHands worker timed out", exc)
            except Exception as exc:
                agent_status, response = "failed", str(exc)
                agent_error = _error("agent", str(exc), exc)
        else: agent_status = "not_configured"
        timings["agent"] = time.monotonic() - agent_started
        stage("repair", "finished", case=case.case_id, agent="OpenHands", result=agent_status, elapsed=f"{timings['agent']:.1f}s", error=(agent_error or {}).get("message"))
        write_text(output / "response.txt", response)
        patch = capture_patch(spaces.repair); write_text(output / "patch.diff", patch.decode(errors="replace"))
        stage("repair", "patch_captured", case=case.case_id, bytes=len(patch))
        if not patch:
            reason = "agent_failed" if agent_error else "no_patch"
            result = RunResult(
                case.case_id, "clean", agent_status, False, False, "not_run", False,
                reason, baseline_summary=_baseline_summary(baseline),
                agent_summary=_agent_summary(events_path, agent_status, timings["agent"], agent_error),
                error=agent_error or _error("agent", "No patch was generated"),
            )
            return finish(result)
        else:
            try:
                assert_safe_patch(patch); apply_patch(spaces.validation, patch)
                stage("repair", "patch_applied", case=case.case_id, bytes=len(patch))
            except PatchError as exc:
                stage("repair", "patch_failed", case=case.case_id, reason=str(exc))
                result = RunResult(
                    case.case_id, "clean", agent_status, True, False, "not_run", False,
                    f"patch_apply_failed: {exc}", baseline_summary=_baseline_summary(baseline),
                    agent_summary=_agent_summary(events_path, agent_status, timings["agent"], agent_error),
                    error=agent_error or _error("patch", str(exc), exc),
                )
                return finish(result)
            else:
                stage("validation", "started", case=case.case_id)
                validation_started = time.monotonic()
                validation = run_post_patch(case, spaces.validation, timeout_seconds=workflow.validation_timeout_seconds, log_dir=output / "logs" / "validation", baseline_failures=case.failing_tests)
                timings["validation"] = time.monotonic() - validation_started
                stage("validation", "finished", case=case.case_id, phase=validation.phase, result=validation.status, passed=validation.passed, reason=validation.reason)
                result = RunResult(
                    case.case_id, "clean", agent_status, True, True, validation.status, validation.passed, validation.reason,
                    baseline_summary=_baseline_summary(baseline),
                    agent_summary=_agent_summary(events_path, agent_status, timings["agent"], agent_error),
                    validation_summary=_validation_summary(validation),
                    error=agent_error or (
                        _error("validation", validation.reason or validation.status)
                        if validation.status == "invalid" or not validation.passed
                        else None
                    ),
                )
                return finish(result)
    finally:
        try:
            if spaces is not None:
                spaces.cleanup()
            # Remove empty legacy/log directories; keep every nonempty artifact.
            for directory in (output / ".tmp", output / "workspaces", output / "validation" / "logs", output / "validation", output / "logs" / "validation", output / "logs"):
                if directory.is_dir() and not directory.is_symlink():
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
        finally:
            configure(None)
