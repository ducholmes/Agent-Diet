"""Inspect pinned serializer evidence; optionally probe the live subscription.

Run from Agent-Diet with PYTHONPATH=src. Default mode never logs in or calls HTTP.
Live responses show acceptance only, not exact semantic equivalence.
"""
import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path

from openhands_adapter.compat.subscription_contract import capability_report
from openhands_adapter.compat.trae_llm_policy import TraeLLMPolicy
from openhands_adapter.compat.audit import register_secrets, sanitize, payload_hash
from openhands_adapter.config import OpenHandsConfig
from openhands_adapter.openhands.runtime import require_pinned_sdk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='gpt-5.6-sol')
    parser.add_argument('--role', choices=('repair', 'compression'), default='repair')
    parser.add_argument('--reference-model')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--field', choices=('control', 'max_tokens', 'temperature', 'n',
                        'reasoning_effort', 'stop', 'cache_control', 'assistant_prefill', 'tools', 'step_wrapper'),
                        action='append', help='live fields to probe individually; default: control')
    args = parser.parse_args()
    require_pinned_sdk()
    reference = args.reference_model or ('claude4-sonnet' if args.role == 'repair' else 'gpt-5-mini-2025-08-07')
    policy = TraeLLMPolicy(args.role, reference)
    report = capability_report(policy, args.model)
    report.update(checked_at=datetime.now(timezone.utc).isoformat(),
                  versions={name: version(name) for name in ('openhands-sdk', 'litellm', 'openai', 'httpx')},
                  live_probes=[])
    if args.live:
        import httpx
        import litellm
        from litellm.llms.custom_httpx.http_handler import HTTPHandler
        from openhands.sdk.llm.options.responses_options import select_responses_options
        from openhands_adapter.openhands.llm import build_llm
        from openhands_adapter.openhands.trae_transport import responses_payload, _drain_response, responses_answer
        llm = build_llm(OpenHandsConfig(model=args.model, transport_conformance='adapted'))
        report['endpoint_identity'] = llm.base_url
        for field in args.field or ['control']:
            captured = []
            def capture(request):
                body = json.loads(request.content)
                captured.append({'body': sanitize(body), 'body_sha256': payload_hash(body)})
            probe = {'field': field, 'semantic_status': 'unverified'}
            messages = [{'role': 'system', 'content': 'Respond briefly.'},
                        {'role': 'user', 'content': 'Say OK.'}]
            tools = []
            if field == 'step_wrapper':
                if args.role != 'compression':
                    parser.error('step_wrapper requires --role compression')
                from openhands_adapter.diet.prompts import build_adapted_compression_messages
                messages = build_adapted_compression_messages(
                    '<step id="0"><result>Repeated line. Repeated line. Keep source.c fix.</result></step>',
                    0, policy)
            if field == 'assistant_prefill':
                messages.append({'role': 'assistant', 'content': 'PREFIX:'})
            if field == 'tools':
                from openhands_adapter.compat.trae_contract import TOOLS
                tools = TOOLS
            if field == 'cache_control':
                messages[0]['content'] = [{'type':'text', 'text':'Respond briefly.',
                                          'cache_control':{'type':'ephemeral'}}]
            fields = {'max_tokens': {'max_output_tokens': 8}, 'temperature': {'temperature': 0.0},
                      'n': {'n': 1}, 'reasoning_effort': {'reasoning': {'effort':'low'}},
                      'stop': {'stop': '</step>'}}
            if field == 'max_tokens':
                messages[1]['content'] = 'Write the numbers from 1 to 200, separated by spaces.'
            if field == 'stop':
                messages[1]['content'] = 'Return exactly: before</step>after'
            try:
                instructions, items, resp_tools = responses_payload(messages, tools)
                options = select_responses_options(llm, {'stream':True}, include=None, store=False)
                options.update(fields.get(field, {}))
                kwargs = llm._build_responses_call_kwargs(items, instructions, resp_tools, options)
                kwargs.update(num_retries=0, max_retries=0)
                register_secrets(kwargs.get('api_key'))
                creds = getattr(llm, '_subscription_credentials', None)
                if creds is not None: register_secrets(creds.access_token, creds.refresh_token)
                with httpx.Client(event_hooks={'request':[capture]}) as client:
                    raw = _drain_response(litellm.responses(**kwargs, client=HTTPHandler(client=client)))
                answer, finish, usage = responses_answer(raw)
                probe.update(endpoint_accepted=True, finish_reason=finish, usage=usage,
                             response_status=raw.get('status') if isinstance(raw, dict) else raw.status)
                if field == 'step_wrapper':
                    import re
                    text = answer.get('content') or ''
                    match = re.fullmatch(r'\s*<step\s+id=["\']0["\']>(.*?)</step>\s*', text, re.DOTALL)
                    probe['wrapper_valid'] = bool(match and match[1].strip() and '<step' not in match[1]
                                                  and '</step>' not in match[1] and finish == 'stop')
            except Exception as error:
                probe.update(endpoint_accepted=False, error_type=type(error).__name__,
                             diagnostic=sanitize(str(error)))
            probe['wire_requests'] = captured
            report['live_probes'].append(probe)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sanitize(report), ensure_ascii=False, indent=2)+'\n')
    print(f'Report written to {args.output}; exact endpoint semantics remain unverified.')
    return 1 if any(not p['endpoint_accepted'] for p in report['live_probes']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
