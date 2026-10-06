"""Offline audit primitives. Metadata never enters the model-visible contract."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit

SCHEMA_VERSION = 1
_context = ContextVar('audit_context', default={})
_secrets: set[str] = set()
# Full arbitrary content is retained only in raw mode, at every event boundary.
CONTENT_FIELDS = frozenset(('payload', 'messages', 'response', 'answer', 'results',
    'steps', 'user_message', 'originals', 'gate_window', 'gate_target', 'parsed_content',
    'bypass_target', 'compressor_context', 'before_text', 'after_text',
    'replacement_message', 'next_repair_messages', 'proposed_text', 'output', 'text', 'action',
    'summary', 'error', 'diagnostic', 'initial_user_prompt', 'original_patch', 'filtered_patch', 'patch'))
SECRET_FIELDS = frozenset(('api_key', 'access_token', 'refresh_token', 'authorization',
                          'cookie', 'password', 'client_secret'))


def canonical_json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def byte_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload_hash(value) -> str:
    return byte_hash(canonical_json_bytes(value))


def safe_payload_hash(value):
    from ..events import record_audit_error
    try:
        return payload_hash(value)
    except Exception as error:
        record_audit_error('payload_hash', error)
        return None


def register_secrets(*values):
    # Read only credentials explicitly selected by config/transport, never env dumps.
    _secrets.update(value for value in values if isinstance(value, str) and value)


def clean_url(value):
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value
    host = parts.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    if parts.port:
        host += f':{parts.port}'
    return urlunsplit((parts.scheme, host, parts.path, '', ''))


def sanitize(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: ('[REDACTED]' if key.lower() in SECRET_FIELDS else
                      clean_url(item) if key in {'base_url', 'endpoint'} and isinstance(item, str) else sanitize(item))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        for secret in sorted(_secrets, key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
    return value


def retention_copy(fields, raw=True):
    result = {}
    for key, value in fields.items():
        if key in CONTENT_FIELDS:
            encoded = canonical_json_bytes(value)
            result[key + '_sha256'] = byte_hash(encoded)
            result[key + '_json_bytes'] = len(encoded)
            clean = sanitize(value)
            if canonical_json_bytes(clean) != encoded:
                result[key + '_redacted'] = True
            if raw:
                result[key] = clean
        else:
            result[key] = sanitize(value)
    return result


def context_fields():
    return dict(_context.get())


@contextmanager
def audit_context(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def request_context(role, *, force_new=False, **fields):
    current = context_fields()
    request_id = str(uuid4()) if force_new or not current.get('request_id') else current['request_id']
    if force_new and current.get('request_id'):
        fields.setdefault('parent_request_id', current['request_id'])
    with audit_context(role=role, request_id=request_id, **fields):
        yield request_id


def export_snapshot(snapshot, *, raw):
    original_redacted = sanitize(snapshot['original_patch']) != snapshot['original_patch']
    capture_raw = raw and not original_redacted
    public = {key: value for key, value in snapshot.items() if key not in {'steps', 'user_message'}}
    public.update(retention_copy({key: snapshot[key] for key in ('steps', 'user_message') if key in snapshot}, capture_raw))
    public['capture_retention'] = 'raw' if capture_raw else 'hash-only'
    if original_redacted:
        public['original_patch_redacted'] = True
        public['redaction_reason'] = 'registered credential in original capture; retain hashes'
    if not capture_raw:
        # Raw test edits are private evidence. A credential in that capture
        # lowers evidence coverage without changing functional submitted bytes.
        for key in ('original_patch', 'filtered_patch'):
            public[key + '_bytes_sha256'] = byte_hash(public.pop(key).encode('utf-8'))
        public['has_filtered_patch'] = bool(snapshot['filtered_patch'].strip())
    return public


def safe_write_json(path, value):
    """Audit-only writes must not change generation success/failure."""
    from ..workflow.artifacts import write_json
    from ..events import record_audit_error
    try:
        write_json(path, sanitize(value))
        return True
    except Exception as error:
        record_audit_error('artifact_write', error)
        return False


def file_hashes(root):
    return {str(p.relative_to(root)): byte_hash(p.read_bytes())
            for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}


def finalize_bundle(output, **metadata):
    from ..events import record_audit_error
    try:
        return _finalize_bundle(output, **metadata)
    except Exception as error:
        record_audit_error('bundle_finalize', error)
        return None


def _finalize_bundle(output, **metadata):
    from ..events import audit_errors, read_events_strict
    path = output / 'audit/manifest.json'
    manifest = json.loads(path.read_text()) if path.is_file() else {}
    records, issues = read_events_strict(output / 'events.jsonl')
    artifacts = {str(p.relative_to(output)): byte_hash(p.read_bytes()) for p in sorted(output.rglob('*'))
                 if p.is_file() and p != path
                 and (p.parent == output or p.is_relative_to(output / 'audit') or p.is_relative_to(output / 'logs'))}
    error_markers = list(output.glob('audit-error-*.json'))
    pending_requests = sorted({e.get('request_id') for e in records if e['type'] == 'trae_llm_request'}
                              - {e.get('request_id') for e in records if e['type'] == 'trae_transport_result'}, key=str)
    pending_calls = sorted({e.get('call_id') for e in records if e['type'] == 'llm_call_started'}
                           - {e.get('call_id') for e in records if e['type'] == 'llm_call_finished'}, key=str)
    missing_boundaries = []
    for boundary in ('run_started', 'run_finished'):
        if not any(e['type'] == boundary for e in records):
            missing_boundaries.append(boundary)
    if (any(e['type'] == 'stage' and e.get('status') == 'worker_started' for e in records)
            and not any(e['type'] == 'worker_finished' for e in records)):
        missing_boundaries.append('worker_finished')
    manifest.update(metadata, artifacts=artifacts, event_records=len(records),
                    audit_errors=audit_errors(), integrity_issues=issues,
                    pending_requests=pending_requests, pending_calls=pending_calls,
                    missing_boundaries=missing_boundaries,
                    audit_complete=not (issues or audit_errors() or error_markers or pending_requests
                                        or pending_calls or missing_boundaries),
                    audit_error_artifacts=[p.name for p in error_markers])
    safe_write_json(path, manifest)


def _verify_adapted_request(request, metadata, errors):
    from .trae_contract import TOOLS, SYS_PROMPT
    from .trae_llm_policy import TraeLLMPolicy
    from .subscription_contract import capability_report
    body = request['payload']
    rid = request.get('request_id')
    policy = TraeLLMPolicy(request['role'], metadata['reference_model'])
    expected_keys = {'instructions', 'input', 'tools', 'stream', 'store'}
    effort = metadata.get('reasoning_effort')
    if effort:
        expected_keys.add('reasoning')
        if body.get('reasoning') != {'effort': effort}:
            errors.append({'contract': 'D07', 'reason': 'adapted reasoning effort mismatch', 'request_id': rid})
    if (request.get('transport_conformance') != 'adapted' or request.get('protocol') != 'responses'
            or set(body) != expected_keys
            or body.get('stream') is not True or body.get('store') is not False):
        errors.append({'contract': 'D07', 'reason': 'invalid adapted Responses body', 'request_id': rid})
    if request.get('actual_model') != metadata.get('wire_model') or request.get('reference_model') != metadata['reference_model']:
        errors.append({'contract': 'D13', 'reason': 'adapted role/model mismatch', 'request_id': rid})
    expected = capability_report(policy, request['actual_model'], reasoning_effort=effort)
    if request.get('capability_report') != expected:
        errors.append({'contract': 'D07', 'reason': 'missing or invalid subscription deviations', 'request_id': rid})
    if request['role'] == 'repair':
        tools = [{'type': 'function', **tool['function'], 'strict': False} for tool in TOOLS]
        if body.get('instructions') != SYS_PROMPT or body.get('tools') != tools:
            errors.append({'contract': 'D01', 'reason': 'adapted repair prompt/tools mismatch', 'request_id': rid})
    elif body.get('tools') != []:
        errors.append({'contract': 'D08', 'reason': 'adapted compression advertised tools', 'request_id': rid})


def verify_bundle(output: Path, *, full=False, expected_profile=None, oracle_path=None):
    """Read-only verification of integrity and structural invariants, not a live oracle."""
    from ..events import read_events_strict
    from ..workflow.trae_patch import validate_snapshot
    errors, limitations = [], []
    adapted = False
    try:
        manifest = json.loads((output / 'audit/manifest.json').read_text())
        records, issues = read_events_strict(output / 'events.jsonl')
        errors.extend({'contract': 'D14', 'reason': issue} for issue in issues)
        if type(manifest.get('schema_version')) is not int or manifest['schema_version'] != SCHEMA_VERSION:
            errors.append({'contract': 'D14', 'reason': 'unsupported manifest schema'})
        if (not manifest.get('audit_complete') or manifest.get('audit_errors') or manifest.get('audit_error_artifacts')
                or list(output.glob('audit-error-*.json'))):
            errors.append({'contract': 'D14', 'reason': 'incomplete audit'})
        if expected_profile and manifest.get('profile') != expected_profile:
            errors.append({'contract': 'D13', 'reason': 'reference profile mismatch'})
        if manifest.get('status') != 'finished':
            errors.append({'contract': 'D14', 'reason': 'missing finished run'})
        for name, expected in manifest.get('artifacts', {}).items():
            p = (output / name).resolve()
            if not p.is_relative_to(output.resolve()) or not p.is_file() or byte_hash(p.read_bytes()) != expected:
                errors.append({'contract': 'D12' if 'patch' in name else 'D14', 'reason': f'artifact mismatch: {name}'})
        if not manifest.get('artifacts') or not {'events.jsonl', 'result.json'} <= manifest.get('artifacts', {}).keys():
            errors.append({'contract': 'D14', 'reason': 'missing required artifacts'})
        for field in ('effective_config', 'validation_policy'):
            if field in manifest and payload_hash(manifest[field]) != manifest.get(field + '_sha256'):
                errors.append({'contract': 'D14', 'reason': f'{field} hash mismatch'})
        conformance = manifest.get('transport_conformance', 'exact')
        adapted = conformance == 'adapted'
        effective_conformance = manifest.get('effective_config', {}).get('openhands', {}).get('transport_conformance', 'exact')
        if conformance not in {'exact', 'adapted'} or effective_conformance != conformance:
            errors.append({'contract': 'D14', 'reason': 'transport conformance mismatch'})
        if not adapted and (manifest.get('auth') == 'subscription' or any(e.get('transport_conformance') == 'adapted' for e in records)):
            errors.append({'contract': 'D07', 'reason': 'subscription adapted evidence cannot establish exact conformance'})
        if manifest.get('profile') == 'generic':
            if any(e.get('run_id') != manifest.get('run_id') or e.get('case_id') != manifest.get('case_id') for e in records):
                errors.append({'contract': 'D14', 'reason': 'event run/case identity mismatch'})
            return {'status': 'fail' if errors else 'unsupported', 'scope': 'bundle_integrity',
                    'errors': errors, 'limitations': ['generic profile does not enforce Trae conformance'],
                    'source_oracle': 'not_run', 'contracts': {f'D{i:02d}': 'unsupported' for i in range(1,15)}}
        from .trae_contract import TOOLS, SYS_PROMPT
        from .trae_llm_policy import TraeLLMPolicy
        if manifest.get('tools') != TOOLS or manifest.get('tools_sha256') != payload_hash(TOOLS):
            errors.append({'contract': 'D03', 'reason': 'advertised tool schema differs from source'})
        reference_path = Path(__file__).with_name('reference_hashes.json')
        reference = json.loads(reference_path.read_text())
        if manifest.get('reference') != reference:
            errors.append({'contract': 'D14', 'reason': 'frozen reference manifest mismatch'})
        adapter_root = Path(__file__).resolve().parents[1]
        repo_root = adapter_root.parents[1]
        reference_root = repo_root / 'artifact/artifact/code/trae_agent'
        for name, expected in reference['sha256'].items():
            source = reference_root / name
            if not source.is_file() or byte_hash(source.read_bytes()) != expected:
                errors.append({'contract': 'D14', 'reason': f'frozen reference source mismatch: {name}'})
        sdk_root = Path(__import__('openhands.sdk', fromlist=['sdk']).__file__).parent
        for name, expected in manifest.get('sdk_source_sha256', {}).items():
            source = (sdk_root / name).resolve()
            if not source.is_relative_to(sdk_root.resolve()) or not source.is_file() or byte_hash(source.read_bytes()) != expected:
                errors.append({'contract': 'D13', 'reason': f'SDK source hash mismatch: {name}'})
        required_sdk_sources = {'agent/base.py', 'agent/agent.py', 'llm/llm.py', 'llm/auth/openai.py',
                                'llm/options/responses_options.py', 'tool/tool.py', 'conversation/impl/local_conversation.py'}
        if set(manifest.get('sdk_source_sha256', {})) != required_sdk_sources:
            errors.append({'contract': 'D13', 'reason': 'missing SDK source hashes'})
        for name, expected in manifest.get('implementation_sha256', {}).items():
            source = (adapter_root / name).resolve()
            if not source.is_relative_to(adapter_root) or not source.is_file() or byte_hash(source.read_bytes()) != expected:
                errors.append({'contract': 'D14', 'reason': f'implementation source hash mismatch: {name}'})
        if manifest.get('implementation_sha256') != file_hashes(adapter_root):
            errors.append({'contract': 'D14', 'reason': 'implementation hash coverage mismatch'})
        if manifest.get('dependency_lock_sha256') != byte_hash((repo_root / 'requirements-openhands.lock').read_bytes()):
            errors.append({'contract': 'D14', 'reason': 'dependency lock hash mismatch'})
        if manifest.get('sdk_version') != '1.49.5':
            errors.append({'contract': 'D13', 'reason': 'SDK version mismatch'})
        if manifest.get('profile') in {'trae_verified', 'trae_multiswe'}:
            cap = 50 if manifest['profile'] == 'trae_verified' else 100
            if manifest.get('logical_turn_cap') != cap or manifest.get('sdk_limit') != cap+1:
                errors.append({'contract': 'D06', 'reason': 'turn/scheduler cap mismatch'})
        controls = manifest.get('runtime_controls', {})
        expected_controls = {'tool_concurrency_limit': 1, 'default_tools': [], 'tools': [],
            'agent_context': None, 'dynamic_context': None, 'condenser': None, 'critic': None,
            'hooks': None, 'stuck_detection': False, 'max_budget_per_run': None,
            'watchdog_seconds': manifest.get('effective_config', {}).get('workflow', {}).get('agent_timeout_seconds'),
            'ambient_plugins': False, 'file_based_agents': False, 'mcp': False}
        for key, value in expected_controls.items():
            if key not in controls or canonical_json_bytes(controls[key]) != canonical_json_bytes(value):
                errors.append({'contract': 'D13', 'reason': f'runtime control mismatch: {key}'})
        config = manifest.get('effective_config', {})
        actual_roles = manifest.get('protocol', {}).get('roles', {})
        repair_config, diet_config = config.get('openhands', {}), config.get('agentdiet', {})
        if adapted:
            from .subscription_contract import capability_report
            if (manifest.get('auth') != 'subscription'
                    or manifest.get('protocol', {}).get('transport_conformance') != 'adapted'):
                errors.append({'contract': 'D07', 'reason': 'adapted manifest transport mismatch'})
            for role, metadata in actual_roles.items():
                if metadata.get('active'):
                    expected = capability_report(TraeLLMPolicy(role, metadata['reference_model']), metadata['actual_model'],
                                                 reasoning_effort=metadata.get('reasoning_effort'))
                    if metadata.get('capability_report') != expected:
                        errors.append({'contract': 'D07', 'reason': f'missing or invalid {role} capability report'})
            if (diet_config.get('enabled') and diet_config.get('mode') == 'ours'
                    and manifest.get('protocol', {}).get('compression_protocol') != 'responses-step-wrapper-v1'):
                errors.append({'contract': 'D08', 'reason': 'adapted compression protocol mismatch'})
        repair_role = actual_roles.get('repair', {})
        if (repair_role.get('actual_model') != repair_config.get('model') or
            repair_role.get('reference_model') != repair_config.get('repair_reference_model')):
            errors.append({'contract': 'D13', 'reason': 'effective repair role mismatch'})
        active = diet_config.get('enabled') and diet_config.get('mode') == 'ours'
        compression_role = actual_roles.get('compression', {})
        actual_compressor = repair_config.get('model') if diet_config.get('compressor_model') == 'inherit' else diet_config.get('compressor_model')
        if (compression_role.get('active') != bool(active) or active and
            (compression_role.get('actual_model') != actual_compressor or
             compression_role.get('reference_model') != diet_config.get('compressor_reference_model'))):
            errors.append({'contract': 'D13', 'reason': 'effective compression role mismatch'})
        for difference in manifest.get('allowed_differences', []):
            validate_difference(difference)
        for event in records:
            if event.get('run_id') != manifest.get('run_id') or event.get('case_id') != manifest.get('case_id'):
                errors.append({'contract': 'D14', 'reason': 'event run/case identity mismatch'})
            for key in CONTENT_FIELDS:
                if key in event and not event.get(key + '_redacted') and key + '_sha256' in event:
                    if payload_hash(event[key]) != event[key + '_sha256']:
                        errors.append({'contract': 'D14', 'reason': f'snapshot mismatch: {key}', 'record_id': event.get('record_id')})
                if event.get(key + '_redacted'):
                    limitations.append('redacted source snapshots cannot establish full parity')
        if not any(e['type'] == 'run_started' for e in records) or not any(e['type'] == 'run_finished' for e in records):
            errors.append({'contract': 'D14', 'reason': 'missing run boundary'})
        requests = {e['request_id']: e for e in records if e['type'] == 'trae_llm_request'}
        terminal = {e.get('request_id') for e in records if e['type'] == 'trae_transport_result'}
        for rid, request in requests.items():
            role = request.get('role')
            roles = manifest.get('protocol', {}).get('roles', {})
            metadata = roles.get(role, {})
            if role not in {'repair', 'compression'} or not metadata.get('active'):
                errors.append({'contract': 'D13', 'reason': 'inactive or unknown request role', 'request_id': rid})
            elif 'payload' in request and not request.get('payload_redacted'):
                body = request['payload']
                params = TraeLLMPolicy(role, metadata['reference_model']).params()
                if adapted:
                    _verify_adapted_request(request, metadata, errors)
                    continue_request_check = False
                else:
                    continue_request_check = True
                if continue_request_check:
                    expected_keys = {'model', 'messages', *params} | ({'tools'} if role == 'repair' else set())
                    if set(body) != expected_keys or any(body.get(k) != v for k, v in params.items()):
                        errors.append({'contract': 'D07', 'reason': 'request policy/body fields mismatch', 'request_id': rid})
                    if (body.get('model') != request.get('actual_model') or request.get('reference_model') != metadata['reference_model']
                            or metadata.get('wire_model') and request.get('actual_model') != metadata['wire_model']):
                        errors.append({'contract': 'D13', 'reason': 'request role/model mapping mismatch', 'request_id': rid})
                    if role == 'repair' and (body.get('tools') != TOOLS or body['messages'][0] != {'role': 'system', 'content': SYS_PROMPT}):
                        errors.append({'contract': 'D01', 'reason': 'repair system/tools differ from source', 'request_id': rid})
            if rid not in terminal:
                errors.append({'contract': 'D14', 'reason': 'pending request', 'request_id': rid})
            attempts = [e for e in records if e['type'] == 'trae_transport_attempt' and e.get('request_id') == rid]
            if [e['attempt'] for e in attempts] != list(range(1, len(attempts)+1)) or not 1 <= len(attempts) <= 12:
                errors.append({'contract': 'D07', 'reason': 'attempt sequence mismatch', 'request_id': rid})
            if any(e.get('body_sha256') != request.get('payload_sha256') or e.get('actual_model') != request.get('actual_model') for e in attempts):
                errors.append({'contract': 'D07', 'reason': 'retry changed model/body', 'request_id': rid})
        repair_requests = [e for e in records if e['type'] == 'trae_llm_request' and e.get('role') == 'repair']
        repair_turns = [e.get('logical_turn') for e in repair_requests]
        if repair_turns != list(range(1, len(repair_requests)+1)):
            errors.append({'contract': 'D06', 'reason': 'repair logical turn sequence mismatch'})
        for event in records:
            if event['type'] == 'diet_reduction_gate':
                old, new = event['old_tokens'], event['new_tokens']
                accepted = old-new >= 400 or new < .8*old
                decisions = [e for e in records if e.get('logical_turn') == event.get('logical_turn') and
                             e.get('step_index') == event.get('step_index') and e['type'] == 'diet_decision']
                if not decisions or (decisions[-1]['status'] == 'accepted') != accepted:
                    errors.append({'contract': 'D10', 'reason': 'reduction decision mismatch'})
            if event['type'] == 'diet_parser_result' and event.get('compression_request_id') in requests:
                request = requests[event['compression_request_id']]
                if all(k in event and not event.get(k+'_redacted') for k in ('compressor_context',)) and 'payload' in request and not request.get('payload_redacted'):
                    from ..diet.prompts import build_compression_messages
                    policy = TraeLLMPolicy('compression', request['reference_model'])
                    if adapted:
                        from ..diet.prompts import build_adapted_compression_messages
                        from ..openhands.trae_transport import responses_payload
                        expected = build_adapted_compression_messages(event['compressor_context'], event['step_index'], policy)
                        instructions, items, _ = responses_payload(expected, [])
                        messages_match = request['payload']['instructions'] == instructions and request['payload']['input'] == items
                    else:
                        expected = build_compression_messages(event['compressor_context'], event['step_index'], policy)
                        messages_match = request['payload']['messages'] == expected
                    if not messages_match:
                        errors.append({'contract': 'D08', 'reason': 'compressor messages differ from source', 'request_id': request['request_id']})
            if adapted and event['type'] == 'diet_parser_result' and event.get('compression_request_id') not in requests:
                errors.append({'contract': 'D08', 'reason': 'missing adapted compression request evidence'})
        if adapted and oracle_path is not None:
            errors.append({'contract': 'D07', 'reason': 'Chat oracle does not establish Responses transport parity'})
        if oracle_path is not None and not adapted:
            expected = json.loads(oracle_path.read_text())
            if manifest.get('retention') != 'raw':
                errors.append({'contract': 'D14', 'reason': 'source oracle requires raw evidence'})
            else:
                actual = {'repair_requests': [e.get('payload') for e in repair_requests],
                          'compression_requests': [e.get('payload') for e in records if e['type']=='trae_llm_request' and e.get('role')=='compression'],
                          'steps': json.loads((output/'contract-result.json').read_text()).get('steps')}
                differences = expected.get('allowed_differences', [])
                for field in ('repair_requests', 'compression_requests', 'steps'):
                    mismatch = first_difference(comparison_projection(actual[field], differences),
                                                comparison_projection(expected[field], differences))
                    if mismatch:
                        errors.append({'contract': 'D09' if field=='steps' else 'D07',
                                       'reason': f'source oracle mismatch: {field}', 'field': mismatch})
        contract = output / 'contract-result.json'
        if contract.is_file():
            snapshot = validate_snapshot(json.loads(contract.read_text()), manifest['profile'])
            if snapshot['run_id'] != manifest['run_id'] or snapshot.get('case_id') != manifest.get('case_id'):
                errors.append({'contract': 'D14', 'reason': 'terminal run/case identity mismatch'})
            if (output / 'patch.diff').read_bytes() != snapshot['patch'].encode('utf-8'):
                errors.append({'contract': 'D12', 'reason': 'submitted patch differs from snapshot'})
            if (output / 'contract-patch.diff').read_bytes() != snapshot['patch'].encode('utf-8'):
                errors.append({'contract': 'D12', 'reason': 'worker patch differs from snapshot'})
            if snapshot['gen'] in {'task_done', 'turn_capped'}:
                if len(repair_requests) != snapshot['turns']:
                    errors.append({'contract': 'D06', 'reason': 'terminal turn/request count mismatch'})
                hooks = [e['logical_turn'] for e in records if e['type']=='trae_after_normal_turn']
                expected_hooks = list(range(1, snapshot['turns']+(1 if snapshot['gen']=='turn_capped' else 0)))
                if hooks != expected_hooks:
                    errors.append({'contract': 'D06', 'reason': 'normal hook/terminal order mismatch'})
            if snapshot['gen'] == 'generation_error':
                errors.append({'contract': 'D11', 'reason': 'generation error is not conformance success'})
        else:
            errors.append({'contract': 'D12', 'reason': 'missing terminal snapshot'})
        if manifest.get('retention') != 'raw':
            limitations.append('hash-only evidence cannot reconstruct source payloads')
        if full and (manifest.get('retention') != 'raw' or limitations):
            errors.append({'contract': 'D14', 'reason': 'full evidence unavailable'})
        limitations.append('Structural verification; source fixture and provider semantics evidence are separate')
    except Exception as error:
        errors.append({'contract': 'D14', 'reason': f'invalid bundle: {type(error).__name__}: {error}'})
    if adapted:
        limitations.append('Adapted Responses transport and compression protocol do not establish exact Trae parity')
    return {'status': 'fail' if errors else 'partial' if adapted else 'pass', 'scope': 'bundle_integrity',
            'transport_status': 'unsupported' if adapted else 'unverified',
            'workflow_status': 'fail' if errors else 'incomplete_evidence',
            'integrity_status': 'fail' if errors else 'pass',
            'errors': errors, 'limitations': sorted(set(limitations)),
            'contracts': {f'D{i:02d}': ('fail' if any(e['contract']==f'D{i:02d}' for e in errors) else
                           'unsupported' if adapted and i in {7, 8, 11} else 'incomplete_evidence') for i in range(1, 15)},
            'source_oracle': 'fail' if errors else 'pass' if oracle_path else 'not_run'}


DIFFERENCE_FIELDS = {
    'model': {('model',), ('actual_model',), ('wire_model',)},
    'harness': {('run_id',), ('conversation_id',), ('producer',), ('record_id',)},
    'input': {('agent_project_path',), ('case_id',), ('relative_id',)},
    'external_validation': {('validation_policy',)},
}


def validate_difference(difference):
    category, field = difference.get('category'), tuple(difference.get('field', []))
    if category not in DIFFERENCE_FIELDS or field not in DIFFERENCE_FIELDS[category]:
        raise ValueError('difference projection outside allowlist')
    if not difference.get('reason') or not difference.get('evidence'):
        raise ValueError('difference requires reason and evidence')


def comparison_projection(value, differences):
    from copy import deepcopy
    value = deepcopy(value)
    for difference in differences:
        validate_difference(difference)
        field = difference['field'][0]
        def project(obj):
            if isinstance(obj, list):
                for item in obj: project(item)
            elif isinstance(obj, dict) and field in obj:
                obj[field] = '[PERMITTED_DIFFERENCE]'
        # Only direct structured fields of the compared records, never strings
        # or nested messages whose content happens to name a model/path.
        project(value)
    return value


def first_difference(actual, expected, path='$'):
    if type(actual) is not type(expected):
        return path
    if isinstance(actual, dict):
        if actual.keys() != expected.keys(): return path + '.keys'
        for key in actual:
            mismatch = first_difference(actual[key], expected[key], path+'.'+key)
            if mismatch: return mismatch
    elif isinstance(actual, list):
        if len(actual) != len(expected): return path+'.length'
        for index, (left, right) in enumerate(zip(actual, expected)):
            mismatch = first_difference(left, right, f'{path}[{index}]')
            if mismatch: return mismatch
    elif isinstance(actual, str) and actual != expected:
        offset = next((i for i,(a,b) in enumerate(zip(actual,expected)) if a != b), min(len(actual),len(expected)))
        return path+f'.character[{offset}]'
    elif actual != expected:
        return path
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--oracle', type=Path)
    parser.add_argument('--expected-profile', choices=('trae_verified', 'trae_multiswe'))
    args = parser.parse_args()
    report = verify_bundle(args.output, full=args.full, expected_profile=args.expected_profile, oracle_path=args.oracle)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report['status'] != 'pass')


if __name__ == '__main__':
    raise SystemExit(main())
