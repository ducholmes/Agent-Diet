"""Child-process OpenHands conversation; wall-clock control stays in process.py."""
from __future__ import annotations

import json
from dataclasses import asdict
import sys
import time
from pathlib import Path
from typing import Any

from ..config import AgentDietConfig, OpenHandsConfig, WorkflowConfig
from ..events import configure, emit, recover_events
from ..token_tracking import aggregate_calls
from ..progress import stage
from .agent import build_agent
from .container import RepairContainer
from .runtime import disable_ambient_discovery, require_pinned_sdk


def _text_content(message: dict[str, Any]) -> str:
    content = message.get("content") or []
    if isinstance(content, str):
        return content
    return "\n".join(
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict) and item.get("text")
    )


def _preview(value: Any, limit: int = 8_000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[-limit:] + "\n[truncated]"
    return value


class ConversationEventLogger:
    """Persist useful agent actions without duplicating the SDK event archive."""

    def __init__(self) -> None:
        self.last_response = ""

    def __call__(self, event: Any) -> None:
        data = event.model_dump(mode="json")
        kind = type(event).__name__
        base = {
            "sdk_event_type": kind,
            "event_id": data.get("id"),
            "parent_id": data.get("parent_id"),
            "source": data.get("source"),
        }
        if kind == "ActionEvent":
            action = data.get("action") or data.get("tool_call") or {}
            if data.get("tool_name") == "finish" and isinstance(action, dict):
                self.last_response = str(action.get("message") or action.get("final_answer") or "")
            emit(
                "agent_action",
                **base,
                tool=data.get("tool_name"),
                tool_call_id=data.get("tool_call_id"),
                action=action,
                summary=data.get("summary"),
            )
            return
        if kind in {"ObservationEvent", "AgentErrorEvent", "UserRejectObservation"}:
            observation = data.get("observation") or {}
            output = observation.get("output") if isinstance(observation, dict) else None
            emit(
                "tool_result" if kind == "ObservationEvent" else "agent_error",
                **base,
                tool=data.get("tool_name"),
                tool_call_id=data.get("tool_call_id"),
                exit_code=observation.get("exit_code") if isinstance(observation, dict) else None,
                timed_out=observation.get("timed_out") if isinstance(observation, dict) else None,
                output=_preview(output),
                error=data.get("error") or data.get("rejection_reason"),
            )
            return
        if kind == "MessageEvent":
            message = data.get("llm_message") or {}
            text = _text_content(message)
            if data.get("source") == "agent" and text:
                self.last_response = text
                emit("agent_message", **base, text=_preview(text, 20_000))
            return
        if kind == "Condensation":
            emit(
                "diet_sdk_condensation_event",
                event_id=data.get("id"),
                forgotten_event_ids=sorted(data.get("forgotten_event_ids") or []),
                summary=data.get("summary"),
            )
            return
        if kind in {"ConversationErrorEvent", "ErrorEvent"}:
            emit("agent_error", **base, error=data.get("error") or data.get("detail") or data)


def run_worker(workspace: Path, prompt: str, output_dir: Path, *, execution_plan: dict, container: RepairContainer, openhands: OpenHandsConfig, diet: AgentDietConfig, workflow: WorkflowConfig) -> str:
    """SDK scheduling with explicit controls and a sanitized archive export."""
    import importlib.metadata
    import os
    import tempfile
    from ..compat.audit import (SCHEMA_VERSION, byte_hash, payload_hash, retention_copy,
        register_secrets, sanitize, safe_write_json, export_snapshot, file_hashes)
    from ..events import identity, record_audit_error
    from ..workflow.artifacts import write_json, write_text
    from ..compat.trae_llm_policy import validate_trae_run
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / 'audit/manifest.json'
    existing = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    # Programmatic worker calls receive the same audit contract as supervised calls.
    from uuid import uuid4
    run_id = existing.get('run_id') or str(uuid4())
    case_id = existing.get('case_id')
    register_secrets(os.environ.get(openhands.api_key_env))
    configure(output_dir / 'events.jsonl', run_id=run_id, case_id=case_id,
              raw=diet.keep_raw_events)
    started = time.monotonic()
    conversation = agent = condenser = None
    archive_temp = None
    worker_status, response = 'failed', ''
    try:
        require_pinned_sdk()
        repair_policy, compression_policy = validate_trae_run(openhands, diet, workflow)
        if not openhands.uses_trae_workflow:
            disable_ambient_discovery()
        stage('repair', 'worker_started', agent='OpenHands', model=openhands.model,
              max_iterations=openhands.scheduling_limit)
        agent, condenser = build_agent(container, openhands, diet, workflow, execution_plan=execution_plan)
        compression_active = diet.enabled and diet.mode == 'ours'
        protocol_manifest = {
            'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'case_id': case_id,
            'profile': openhands.reference_profile,
            'transport_conformance': openhands.transport_conformance,
            'roles': {
                'repair': {'active': True, 'actual_model': openhands.model,
                    'reference_model': openhands.repair_reference_model,
                    'wire_model': agent.llm._provider_info.model if repair_policy else None,
                    'params': repair_policy.params() if repair_policy else None},
                'compression': {'active': compression_active,
                    'shared_actual_model': compression_active and diet.compressor_model == 'inherit',
                    'actual_model': (openhands.model if diet.compressor_model == 'inherit' else diet.compressor_model) if compression_active else None,
                    'reference_model': diet.compressor_reference_model,
                    'wire_model': condenser.compressor.llm._provider_info.model if compression_active and hasattr(condenser.compressor, 'llm') else None,
                    'params': compression_policy.params() if compression_policy else None}},
            'versions': {package: importlib.metadata.version(package) for package in
                         ('openhands-sdk', 'litellm', 'openai', 'httpx', 'tiktoken', 'lz4')},
            'transport': ('SDK-auth/Responses-adapted' if openhands.transport_conformance == 'adapted'
                          else 'SDK-auth/OpenAI-chat-raw' if openhands.uses_trae_workflow else 'SDK-public/generic'),
            'confirmed_endpoint_capabilities': list(openhands.trae_capabilities),
            'provider_semantics_verified_by_adapter': False,
            'retry': {'total_attempts': 12, 'inner_retries': 0, 'local_response_cache': False} if openhands.uses_trae_workflow else 'SDK defaults',
        }
        if openhands.transport_conformance == 'adapted':
            from ..compat.subscription_contract import capability_report
            for role, policy in (('repair', repair_policy), ('compression', compression_policy)):
                if policy is not None:
                    metadata = protocol_manifest['roles'][role]
                    metadata['reasoning_effort'] = openhands.reasoning_effort
                    metadata['capability_report'] = capability_report(
                        policy, metadata['actual_model'], reasoning_effort=openhands.reasoning_effort)
            protocol_manifest['compression_protocol'] = 'responses-step-wrapper-v1' if compression_active else None
        safe_write_json(output_dir / 'protocol-manifest.json', protocol_manifest)
        event_logger = ConversationEventLogger()
        from openhands.sdk import LocalConversation
        if openhands.uses_trae_workflow:
            from .trae_agent import TraeLocalConversation
            LocalConversation = TraeLocalConversation
        # Stock SDK files are staged outside repair and exported after close through
        # a credential sanitizer. New UUID prevents implicit reuse of old state.
        if diet.keep_raw_events:
            try:
                archive_temp = tempfile.TemporaryDirectory(prefix='.sdk-staging-', dir=output_dir / 'audit')
            except OSError as error:
                record_audit_error('sdk_archive_setup', error)
        stage('repair', 'conversation_started', agent='OpenHands', model=openhands.model)
        conversation = LocalConversation(
            agent=agent, workspace=workspace, callbacks=[event_logger], visualizer=None,
            max_iteration_per_run=openhands.scheduling_limit,
            stuck_detection=not openhands.uses_trae_workflow,
            max_budget_per_run=None, hook_config=None,
            persistence_dir=Path(archive_temp.name) if archive_temp else None,
            delete_on_close=False if archive_temp else True,
        )
        if openhands.uses_trae_workflow:
            from ..compat.trae_contract import TOOLS
            reference = Path(__file__).resolve().parents[1] / 'compat/reference_hashes.json'
            sdk_root = Path(__import__('openhands.sdk', fromlist=['sdk']).__file__).parent
            hashes = {name: byte_hash((sdk_root / name).read_bytes()) for name in
                ('agent/base.py', 'agent/agent.py', 'llm/llm.py', 'llm/auth/openai.py',
                 'llm/options/responses_options.py', 'tool/tool.py', 'conversation/impl/local_conversation.py')}
            controls = {'tool_concurrency_limit': agent.tool_concurrency_limit,
                'default_tools': agent.include_default_tools, 'tools': agent.tools,
                'agent_context': agent.agent_context, 'dynamic_context': agent.dynamic_context,
                'condenser': agent.condenser, 'critic': agent.critic,
                'hooks': None if conversation._hook_processor is None else 'active',
                'stuck_detection': conversation._stuck_detector is not None,
                'max_budget_per_run': conversation.max_budget_per_run,
                'watchdog_seconds': workflow.agent_timeout_seconds,
                'sdk_limit': conversation.max_iteration_per_run,
                'ambient_plugins': False, 'file_based_agents': False, 'mcp': False}
            input_event = next((event for event in reversed(recover_events(output_dir / 'events.jsonl'))
                                if event.get('type') == 'run_started'), {})
            effective_config = sanitize({'openhands': asdict(openhands), 'agentdiet': asdict(diet), 'workflow': asdict(workflow)})
            manifest = {'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'case_id': case_id,
                'conversation_id': str(conversation.state.id),
                'input_mapping': {key: input_event.get(key) for key in ('case_id', 'issue_source', 'issue_encoding', 'issue_sha256')},
                'reference': json.loads(reference.read_text()),
                'sdk_version': importlib.metadata.version('openhands-sdk'), 'sdk_source_sha256': hashes,
                'implementation_sha256': file_hashes(Path(__file__).resolve().parents[1]),
                'dependency_lock_sha256': byte_hash((Path(__file__).resolve().parents[3] / 'requirements-openhands.lock').read_bytes()),
                'diet_effective_config': asdict(diet), 'model': openhands.model, 'auth': openhands.auth,
                'transport': f'SDKRawTransport/{agent._transport.protocol}',
                'transport_conformance': openhands.transport_conformance,
                'profile': openhands.reference_profile, 'logical_turn_cap': openhands.scheduling_limit-1,
                'sdk_limit': openhands.scheduling_limit, 'runtime_controls': controls,
                'agent_project_path': str(workspace.resolve()), 'protocol': protocol_manifest,
                'initial_user_prompt_bytes_sha256': byte_hash(prompt.encode('utf-8')),
                **retention_copy({'initial_user_prompt': prompt}, diet.keep_raw_events),
                'tools': TOOLS, 'tools_sha256': payload_hash(TOOLS),
                'effective_config': effective_config, 'effective_config_sha256': payload_hash(effective_config),
                'diet_conformance': json.loads((reference.parent / 'diet_conformance.json').read_text()),
                'capture_boundary': 'client_body_object; HTTP serialization independently tested',
                'sdk_archive': {'enabled': bool(archive_temp), 'export': 'sanitized_after_close',
                    'effective_staging_path': str(conversation.state.persistence_dir) if archive_temp else None},
                'levels': {'local': 'fixture_evidence_separate', 'container': 'not_run', 'live': 'not_run'},
            }
            safe_write_json(output_dir / 'contract-manifest.json', manifest)
            safe_write_json(manifest_path, {**existing, **manifest, 'retention': 'raw' if diet.keep_raw_events else 'hash-only',
                                         'status': 'running', 'audit_complete': False})
        conversation.send_message(prompt)
        conversation.run()
        from openhands.sdk.conversation.state import ConversationExecutionStatus
        if openhands.uses_trae_workflow and conversation.state.execution_status != ConversationExecutionStatus.FINISHED:
            raise RuntimeError(f'OpenHands stopped with {conversation.state.execution_status}')
        response = event_logger.last_response
        worker_status = 'completed'
        stage('repair', 'conversation_finished', agent='OpenHands', model=openhands.model)
    except Exception as error:
        emit('agent_error', phase='worker', error=str(error), error_type=type(error).__name__,
             cause_type=type(error.__cause__).__name__ if error.__cause__ is not None else None)
        raise
    finally:
        generation_error = sys.exc_info()[1]
        try:
            # Export failure status too; WIP recovery belongs to host and never
            # masquerades as an accepted terminal snapshot.
            if openhands.uses_trae_workflow and conversation is not None:
                snapshot = conversation.state.agent_state.get('trae_contract')
                if snapshot is not None:
                    snapshot = dict(snapshot)
                    if not snapshot.get('gen'):
                        snapshot.update(gen='generation_error', patch_origin='error_wip', patch='',
                                        original_patch='', filtered_patch='')
                    snapshot.update(schema_version=SCHEMA_VERSION, run_id=run_id, case_id=case_id)
                    snapshot['sdk_execution_status'] = str(conversation.state.execution_status)
                    snapshot['stop_reason'] = snapshot['gen']
                    snapshot['error_type'] = type(generation_error).__name__ if generation_error else None
                    snapshot['cause_type'] = type(generation_error.__cause__).__name__ if generation_error and generation_error.__cause__ else None
                    # Functional artifacts use the exact bytes that runner consumes.
                    write_json(output_dir / 'contract-result.json', export_snapshot(snapshot, raw=diet.keep_raw_events))
                    write_text(output_dir / 'contract-patch.diff', snapshot['patch'])
                    emit('trae_generation_finished', gen=snapshot['gen'], turns=snapshot['turns'],
                         reference_profile=openhands.reference_profile, patch_origin=snapshot['patch_origin'])
            if conversation is not None:
                conversation.close()
            elif agent is not None:
                agent.close()
        except Exception as error:
            worker_status = 'failed'
            emit('agent_error', phase='cleanup', error=str(error), error_type=type(error).__name__)
            if generation_error is None:
                raise
        finally:
            if archive_temp is not None:
                try:
                    staging = Path(archive_temp.name)
                    for path in staging.rglob('*'):
                        if not path.is_file():
                            continue
                        dest = output_dir / 'audit/sdk' / path.relative_to(staging)
                        try:
                            value = json.loads(path.read_text())
                        except ValueError:
                            value = None
                        if value is not None:
                            safe_write_json(dest, value)
                        else:
                            # SDK event logs can be JSONL; sanitize each parsed record.
                            lines = [sanitize(json.loads(line)) for line in path.read_text().splitlines() if line]
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_text(''.join(json.dumps(line, ensure_ascii=False)+'\n' for line in lines))
                    emit('sdk_archive_exported', conversation_id=str(conversation.state.id) if conversation else None)
                except Exception as error:
                    record_audit_error('sdk_archive_export', error)
                finally:
                    archive_temp.cleanup()
            if condenser is not None:
                finish_audit = getattr(getattr(agent, 'condenser', None), 'finish_audit', None)
                if callable(finish_audit):
                    finish_audit()
                metrics = condenser.metrics
                compression = aggregate_calls(recover_events(output_dir / 'events.jsonl'))['compression']
                if compression['calls'] and not openhands.uses_trae_workflow:
                    metrics.update(compression_llm_calls=compression['calls'],
                        analysis_prompt_tokens=compression['input_tokens'], analysis_completion_tokens=compression['output_tokens'],
                        compression_total_tokens=compression['total_tokens'])
                if openhands.uses_trae_workflow:
                    metrics['reference_diet_metrics'] = {**condenser.metrics,
                        'analysis_cost_tokens': metrics['compression_total_tokens'], 'erase_tot_count': metrics['erase_count']}
                    metrics['provider_compression_usage'] = compression
                emit('diet_metrics', metrics=metrics, elapsed_seconds=round(time.monotonic()-started, 3))
            emit('worker_finished', elapsed_seconds=round(time.monotonic()-started, 3), status=worker_status)
    return str(response)


def main() -> int:
    """Read a serializable config from process.py and write the final response."""
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    prompt = sys.stdin.read()
    output = Path(config["output_dir"])
    container = RepairContainer(config["container_name"], Path(config["workspace"]), config["image"], config["runtime"])
    response = run_worker(
        Path(config["workspace"]), prompt, output, container=container, execution_plan=config["execution_plan"],
        openhands=OpenHandsConfig.from_mapping(config["openhands"]), diet=AgentDietConfig.from_mapping(config["agentdiet"]), workflow=WorkflowConfig.from_mapping(config["workflow"]),
    )
    from ..compat.audit import sanitize
    Path(config["response_path"]).write_text(sanitize(response), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
