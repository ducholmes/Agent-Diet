"""One-case orchestrator. Validation is isolated from agent implementation."""
from __future__ import annotations
from collections.abc import Callable
from pathlib import Path
import time
import json
import shutil
import os
from uuid import uuid4
from dataclasses import asdict
from ..compat.audit import (SCHEMA_VERSION, byte_hash, payload_hash, sanitize, register_secrets,
                            safe_write_json, finalize_bundle, verify_bundle)
from ..config import DEFAULT_OUTPUT_ROOT, WorkflowConfig
from ..events import configure, emit, latest, recover_events
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
    value = {"phase": phase, "message": sanitize(message)}
    if exc is not None:
        value["type"] = type(exc).__name__
    return value


def _agent_summary(events_path: Path, status: str, elapsed: float, error: dict | None) -> dict:
    summary = {
        "status": status,
        "elapsed_seconds": round(elapsed, 3),
        "error": error,
    }
    process_event = next((e for e in reversed(recover_events(events_path)) if e.get("type") == "agent_process"), None)
    if process_event:
        summary.update(
            returncode=process_event.get("returncode"),
            timed_out=process_event.get("timed_out"),
            worker_elapsed_seconds=process_event.get("elapsed_seconds"),
        )
    return summary


def run_case(case: CaseSpec | Path | str, workflow: WorkflowConfig, *, selector: str | None = None, agent_runner: AgentRunner | None = None, reference_profile: str = "generic", run_config=None) -> RunResult:
    if not isinstance(case, CaseSpec): case = load_case(case, selector)
    workflow.validate()
    if run_config is not None:
        register_secrets(os.environ.get(run_config.openhands.api_key_env))
    output = (workflow.output_root or DEFAULT_OUTPUT_ROOT).resolve() / case.relative_id; output.mkdir(parents=True, exist_ok=True)
    events_path = output / "events.jsonl"
    run_id = str(uuid4())
    raw = run_config.agentdiet.keep_raw_events if run_config is not None else True
    configure(events_path, reset=True, run_id=run_id, case_id=case.case_id, raw=raw)
    # A rerun replaces this case's artifacts. Clear prior results too, so an
    # early failure cannot leave a previous patch or response beside new events.
    for obsolete in (
        output / "baseline.json", output / "validation.json", output / "diet-events.jsonl",
        output / "result.json", output / "patch.diff", output / "response.txt",
        output / "contract-result.json", output / "contract-patch.diff", output / "contract-manifest.json",
        output / "protocol-manifest.json", output / "recovery-patch.diff",
    ):
        obsolete.unlink(missing_ok=True)
    for directory in (output / 'audit', output / 'logs'):
        if directory.is_symlink():
            directory.unlink()
        elif directory.is_dir():
            shutil.rmtree(directory)
    for marker in output.glob('audit-error-*.json'):
        marker.unlink()
    effective = sanitize(run_config.to_dict()) if run_config is not None else {'workflow': sanitize(asdict(workflow))}
    manifest = {'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'case_id': case.case_id,
        'relative_id': case.relative_id, 'profile': reference_profile, 'status': 'running',
        'transport_conformance': run_config.openhands.transport_conformance if run_config else 'exact',
        'retention': 'raw' if raw else 'hash-only', 'audit_complete': False,
        'effective_config': effective, 'effective_config_sha256': payload_hash(effective),
        'case_config_bytes_sha256': byte_hash(case.config_path.read_bytes()) if case.config_path.is_file() else None,
        'issue_bytes_sha256': case.issue_sha256,
        'validation_policy': sanitize({'setup': case.setup_commands, 'build': case.build_commands,
            'target': case.target_test_commands, 'regression': case.regression_test_commands,
            'timeout_seconds': workflow.validation_timeout_seconds}),
        'input': {'source': str(case.source_project), 'runtime': case.environment.runtime,
                  'image': case.environment.image, 'resolved_image_id': None},
        'levels': {'local': 'fixture_evidence_separate', 'container': 'not_run', 'live': 'not_run'},
        'allowed_differences': [],
    }
    manifest['validation_policy_sha256'] = payload_hash(manifest['validation_policy'])
    safe_write_json(output / 'audit/manifest.json', manifest)
    started = time.monotonic()
    timings: dict[str, float] = {}
    emit("run_started", case_id=case.case_id, reference_profile=reference_profile,
         issue_source=case.issue_source, issue_encoding=case.issue_encoding, issue_sha256=case.issue_sha256)
    workspace_parent = output / ("workspaces" if workflow.keep_workspaces else ".tmp")
    workspace_parent.mkdir(parents=True, exist_ok=True)
    spaces = None
    patch_origin = None

    def finish(result: RunResult) -> RunResult:
        result.elapsed_seconds = round(time.monotonic() - started, 3)
        timings["total"] = result.elapsed_seconds
        result.timings = {key: round(value, 3) for key, value in timings.items()}
        events = recover_events(events_path)
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
        result.run_id = run_id
        result.patch_origin = patch_origin
        write_json(output / 'result.json', sanitize(result))
        finalize_bundle(output, status='finished', generation_status=result.agent,
                        validation_status=result.validation, resolved=result.resolved,
                        patch_origin=patch_origin,
                        runtime_evidence=next((e for e in reversed(events) if e['type']=='repair_runtime'), None),
                        baseline_commit=next((e.get('baseline_commit') for e in reversed(events) if e['type']=='repair_baseline'), None))
        report = verify_bundle(output, full=raw, expected_profile=reference_profile if reference_profile != 'generic' else None)
        result.audit_status = report['status']
        safe_write_json(output / 'audit/conformance-report.json', report)
        write_json(output / 'result.json', sanitize(result))
        finalize_bundle(output, status='finished')
        return result

    current_phase = 'preflight'
    try:
        if run_config is not None:
            run_config.validate()
            if run_config.openhands.reference_profile != reference_profile:
                raise ValueError('run config/profile mismatch')
            from ..compat.trae_llm_policy import validate_trae_run
            validate_trae_run(run_config.openhands, run_config.agentdiet, workflow)
        if reference_profile != 'generic' and case.environment.runtime != 'docker':
            raise ValueError('exact profile requires Docker runtime')
        current_phase = 'workspace_setup'
        spaces = create_workspaces(
            case.source_project,
            parent=workspace_parent,
            keep=workflow.keep_workspaces,
        )
        stage("baseline", "starting", case=case.case_id)
        baseline_started = time.monotonic()
        current_phase = 'baseline'
        baseline = run_baseline(case, spaces.repair, spaces.validation, timeout_seconds=workflow.validation_timeout_seconds)
        timings["baseline"] = time.monotonic() - baseline_started
        stage("baseline", "finished", case=case.case_id, result="clean" if baseline.clean else "failed", elapsed=f"{timings['baseline']:.1f}s", reason=baseline.reason)
        if baseline.clean:
            import subprocess
            try:
                baseline_id = subprocess.check_output(('git', '-C', str(spaces.repair), 'rev-parse', 'HEAD'), stderr=subprocess.DEVNULL).decode().strip()
            except subprocess.CalledProcessError:
                baseline_id = None
            emit('repair_baseline', baseline_commit=baseline_id)
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
        write_text(output / "response.txt", sanitize(response))
        if reference_profile != "generic":
            from .trae_patch import capture_filtered_patch, validate_snapshot
            contract = output / 'contract-result.json'
            if contract.is_file():
                try:
                    snapshot = validate_snapshot(json.loads(contract.read_text(encoding='utf-8')), reference_profile)
                    if snapshot['run_id'] != run_id or snapshot.get('case_id') != case.case_id:
                        raise ValueError('contract run/case identity mismatch')
                    worker_patch = output / 'contract-patch.diff'
                    if not worker_patch.is_file() or worker_patch.read_bytes() != snapshot['patch'].encode('utf-8'):
                        raise ValueError('worker patch artifact mismatch')
                except (ValueError, TypeError, KeyError, OSError) as error:
                    emit('contract_artifact_error', error_type=type(error).__name__, error=str(error))
                    result = RunResult(case.case_id, 'clean', 'contract_artifact_error', False, False,
                        'not_run', False, 'contract_artifact_error', error=_error('contract_artifact', str(error), error))
                    return finish(result)
                agent_status = snapshot['gen']
                patch_origin = snapshot['patch_origin']
                patch = snapshot['patch'].encode('utf-8')
                if snapshot['gen'] == 'generation_error' and not patch.strip():
                    patch = capture_filtered_patch(spaces.repair).encode('utf-8')
                    patch_origin = 'error_wip'
                    write_text(output / 'recovery-patch.diff', patch.decode('utf-8'))
            else:
                if agent_status == 'completed':
                    emit('contract_artifact_error', error_type='MissingSnapshot')
                    result = RunResult(case.case_id, 'clean', 'contract_artifact_error', False, False,
                        'not_run', False, 'contract_artifact_error', error=_error('contract_artifact','Missing terminal contract snapshot'))
                    return finish(result)
                patch_origin = 'error_wip' if agent_status == 'failed' else 'harness_abort_wip'
                patch = capture_filtered_patch(spaces.repair).encode('utf-8')
                write_text(output / 'recovery-patch.diff', patch.decode('utf-8'))
            emit('submission_policy', patch_origin=patch_origin,
                 evaluate_error_or_abort_wip=True, generation_status=agent_status)
        else:
            patch_origin = "generic_capture"
            patch = capture_patch(spaces.repair)
        write_text(output / "patch.diff", patch.decode(errors="replace"))
        emit('submitted_patch', patch_origin=patch_origin, patch_bytes_sha256=byte_hash(patch), bytes=len(patch))
        stage("repair", "patch_captured", case=case.case_id, bytes=len(patch))
        if not patch.strip():
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
                emit('validation_patch_applied', patch_bytes_sha256=byte_hash(patch), isolated_copy=True)
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
    except Exception as error:
        emit('run_failed', phase=current_phase, error_type=type(error).__name__)
        result = RunResult(case.case_id, 'failed', 'preflight_error' if current_phase == 'preflight' else 'harness_error', False, False, 'not_run', False,
                           'harness_error', error=_error(current_phase, str(error), error))
        finish(result)
        raise
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
