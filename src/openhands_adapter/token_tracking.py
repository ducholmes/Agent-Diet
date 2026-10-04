"""Token-only accounting for SDK requests, including incomplete worker runs."""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from .diet.core import count_tokens
from .events import emit

_role: ContextVar[tuple[str, int | None]] = ContextVar('llm_role', default=('repair', None))
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'total_tokens', 'cached_input_tokens', 'cache_write_tokens', 'reasoning_tokens')


def value(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def integer(obj: Any, key: str) -> int | None:
    item = value(obj, key)
    return item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else None


def response_tokens(raw: Any) -> dict[str, Any]:
    usage = value(raw, 'usage')
    input_tokens = integer(usage, 'prompt_tokens')
    if input_tokens is None:
        input_tokens = integer(usage, 'input_tokens')
    output_tokens = integer(usage, 'completion_tokens')
    if output_tokens is None:
        output_tokens = integer(usage, 'output_tokens')
    available = input_tokens is not None and output_tokens is not None
    input_details = value(usage, 'prompt_tokens_details') or value(usage, 'input_tokens_details')
    output_details = value(usage, 'completion_tokens_details') or value(usage, 'output_tokens_details')
    cached = integer(input_details, 'cached_tokens')
    if cached is None:
        cached = integer(usage, 'cache_read_input_tokens')
    cache_write = integer(input_details, 'cache_write_tokens')
    if cache_write is None:
        cache_write = integer(input_details, 'cache_creation_tokens')
    if cache_write is None:
        cache_write = integer(usage, 'cache_creation_input_tokens')
    total = integer(usage, 'total_tokens')
    if total is None and available:
        total = input_tokens + output_tokens
    return {
        'usage_status': 'reported' if available else 'unknown',
        'input_tokens': input_tokens, 'output_tokens': output_tokens, 'total_tokens': total,
        'cached_input_tokens': cached, 'cache_write_tokens': cache_write,
        'reasoning_tokens': integer(output_details, 'reasoning_tokens'),
    }


@contextmanager
def compression_call(step_index: int):
    token = _role.set(('compression', step_index))
    try:
        yield
    finally:
        _role.reset(token)


def aggregate_calls(events: list[dict[str, Any]]) -> dict[str, Any]:
    calls: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get('type') in {'llm_call_started', 'llm_call_finished'}:
            calls[event['call_id']] = event

    def group(records):
        known = {field: sum(r[field] for r in records if isinstance(r.get(field), int)) for field in TOKEN_FIELDS}
        totals = {field: known[field] if all(isinstance(r.get(field), int) for r in records) else None for field in TOKEN_FIELDS}
        reported = sum(r.get('usage_status') == 'reported' for r in records)
        return {
            **totals, 'known_tokens': known, 'calls': len(records),
            'reported_calls': reported, 'unknown_usage_calls': len(records) - reported,
            'pending_calls': sum(r.get('status') == 'running' for r in records),
            'failed_calls': sum(r.get('status') == 'error' for r in records),
            'complete': reported == len(records), 'available': reported > 0,
            'models': sorted({r['model'] for r in records if r.get('model')}),
        }

    records = list(calls.values())
    return {
        'repair': group([r for r in records if r['role'] == 'repair']),
        'compression': group([r for r in records if r['role'] == 'compression']),
        'total': group(records), 'source': 'per_request_sdk_telemetry',
    }


def summarize_diet(events: list[dict[str, Any]], usage: dict[str, Any]) -> dict[str, Any]:
    """Recover counters from events even if the worker's final snapshot is absent."""
    snapshots = [event for event in events if event.get('type') == 'diet_metrics']
    metrics = dict(snapshots[-1].get('metrics', {})) if snapshots else {}
    compression = usage['compression']
    changes = [event for event in events if event.get('type') == 'diet_step_change' and event.get('status') == 'accepted']
    metrics.update(
        compression_llm_calls=compression['calls'],
        analysis_prompt_tokens=compression['input_tokens'],
        analysis_completion_tokens=compression['output_tokens'],
        compression_total_tokens=compression['total_tokens'],
        analysis_count=sum(event.get('type') == 'diet_analysis_started' for event in events),
        erase_count=len(changes),
        erase_in_tokens=sum(event['before_token_estimate'] for event in changes),
        erase_out_tokens=sum(event['after_token_estimate'] for event in changes),
    )
    metrics['step_content_reduction_tokens'] = metrics['erase_in_tokens'] - metrics['erase_out_tokens']
    rejected: dict[str, int] = {}
    for event in events:
        if event.get('type') == 'diet_rejection':
            reason = event['reason']
            rejected[reason] = rejected.get(reason, 0) + 1
    metrics['rejected'] = rejected
    condensations = [event for event in events if event.get('type') == 'diet_sdk_condensation']
    metrics['reminder_token_estimate'] = sum(event.get('reminder_token_estimate', 0) for event in condensations)
    metrics['view_reduction_token_estimate'] = sum(event['before_token_estimate'] - event['expected_after_token_estimate'] for event in condensations)
    checks = [event for event in events if event.get('type') == 'diet_application_check']
    metrics['application_checks'] = {
        status: sum(event['status'] == status for event in checks)
        for status in sorted({event['status'] for event in checks})
    }
    return metrics


def install_token_tracking(llm: Any, diet_metrics: Any) -> None:
    """Attach token-only telemetry once to each actual SDK LLM instance."""
    try:
        from openhands.sdk.llm import LLM
        from openhands.sdk.llm.utils.telemetry import Telemetry
        from pydantic import PrivateAttr
    except ImportError:
        return

    if not isinstance(llm, LLM):
        return
    if getattr(llm.telemetry, '_agent_diet_tracking', False) is True:
        return

    class TokenTelemetry(Telemetry):
        _agent_diet_tracking: bool = PrivateAttr(default=True)
        _pending: dict[str, Any] | None = PrivateAttr(default=None)

        def _compute_cost(self, *args, **kwargs):
            return None

        def on_request(self, telemetry_ctx):
            if self._pending is not None:
                self._finish(status='error', error_type='RetryWithoutUsage')
            super().on_request(telemetry_ctx)
            role, step_index = _role.get()
            context = telemetry_ctx or {}
            payload = {k: context[k] for k in ('messages', 'input', 'instructions', 'tools') if k in context}
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
            reminder_text = []
            def reminders(obj):
                if isinstance(obj, str) and obj.startswith((
                    '(System reminder: compressed for better efficiency)',
                    '(System reminder: long content deleted for better efficiency)',
                )):
                    reminder_text.append(obj)
                elif isinstance(obj, dict):
                    for item in obj.values():
                        reminders(item)
                elif isinstance(obj, (list, tuple)):
                    for item in obj:
                        reminders(item)
            reminders(payload)
            self._pending = {
                'call_id': str(uuid4()), 'role': role, 'model': self.model_name,
                'step_index': step_index, 'response_id': None,
                'usage_status': 'unknown', 'status': 'running',
                'context_token_estimate': count_tokens(encoded) if payload else None,
                'reminder_message_token_estimate': sum(count_tokens(t) for t in reminder_text),
                'context_estimate_encoding': 'gpt-4o',
                'context_window': context.get('context_window'),
                **{field: None for field in TOKEN_FIELDS},
            }
            if role == 'compression':
                diet_metrics.compression_llm_calls += 1
            emit('llm_call_started', **self._pending)

        def _finish(self, **fields):
            if self._pending is not None:
                emit('llm_call_finished', **{**self._pending, **fields})
                self._pending = None

        def on_response(self, resp, raw_resp=None, provider_info=None):
            choices = value(resp, 'choices', []) or []
            reason = value(choices[0], 'finish_reason') if choices else None
            status = value(resp, 'status')
            self._finish(
                **response_tokens(resp), response_id=value(resp, 'id'),
                response_model=value(resp, 'model'), finish_reason=reason, response_status=status,
                incomplete_reason=value(value(resp, 'incomplete_details'), 'reason'),
                response_error_code=value(value(resp, 'error'), 'code'),
                status='error' if reason == 'error' or status in {'failed', 'cancelled'} else 'responded',
            )
            return super().on_response(resp, raw_resp=raw_resp, provider_info=provider_info)

        def on_error(self, error):
            self._finish(status='error', error_type=type(error).__name__)
            return super().on_error(error)

    llm._telemetry = TokenTelemetry(model_name=llm.model, metrics=llm.metrics, log_enabled=True)
