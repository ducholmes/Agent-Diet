"""Offline contract evidence for the Trae subscription adaptation."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from openhands_adapter.config import OpenHandsConfig, AgentDietConfig, WorkflowConfig, RunConfig
from openhands_adapter.compat.trae_llm_policy import TraeLLMPolicy, validate_trae_run
from openhands_adapter.compat.audit import verify_bundle, payload_hash, finalize_bundle
from openhands_adapter.events import configure, read_events
from openhands_adapter.openhands.trae_transport import SDKRawTransport
from openhands_adapter.openhands.compressor import build_compressor
from openhands_adapter.compat.trae_contract import SYS_PROMPT, TOOLS, MessageManager
from test_trae_contract_subscription import subscription_llm, provider_response, sse_events, decode_wire_input
from trae_reference import response


class AdaptedTests(unittest.TestCase):
    def setUp(self):
        identity = patch('openhands.sdk.llm.auth.openai._extract_chatgpt_account_id', return_value='test-account')
        identity.start()
        self.addCleanup(identity.stop)
        self.addCleanup(lambda: configure(None))

    def test_config_matrix_and_preflight(self):
        from openhands_adapter.cli import build_parser, _override
        args = build_parser().parse_args(['--input','/tmp','--model','gpt-5.6-sol','--auth','subscription','--transport-conformance','adapted',
                                          '--diet-mode','ours','--compressor-model','gpt-5.6-luna'])
        parsed = _override(RunConfig(), args, None)
        self.assertEqual(parsed.openhands.transport_conformance, 'adapted')
        with patch.dict('os.environ', {'OPENHANDS_TRANSPORT_CONFORMANCE':'adapted',
                                     'OPENHANDS_AUTH':'subscription', 'OPENHANDS_REFERENCE_PROFILE':'trae_verified'}):
            self.assertEqual(OpenHandsConfig.from_env().transport_conformance, 'adapted')
        for profile in ('trae_verified', 'trae_multiswe'):
            cfg = OpenHandsConfig(reference_profile=profile, transport_conformance='adapted')
            cfg.validate()
            self.assertTrue(cfg.uses_trae_workflow)
            self.assertFalse(cfg.exact_trae)
            self.assertEqual(cfg.scheduling_limit, 51 if profile == 'trae_verified' else 101)
            policies = validate_trae_run(cfg, AgentDietConfig(compressor_model='gpt-5.6-luna'), WorkflowConfig())
            self.assertEqual([p.role for p in policies], ['repair', 'compression'])
            run = RunConfig(openhands=cfg, agentdiet=AgentDietConfig(compressor_model='gpt-5.6-luna'))
            self.assertEqual(RunConfig.from_mapping(run.to_dict()).to_dict(), run.to_dict())
        for cfg in (OpenHandsConfig(reference_profile='generic', transport_conformance='adapted'),
                    OpenHandsConfig(auth='api-key', transport_conformance='adapted')):
            with self.assertRaises(ValueError): cfg.validate()
        with self.assertRaisesRegex(ValueError, 'subscription'):
            validate_trae_run(OpenHandsConfig(), AgentDietConfig(mode='skip'))

    def test_wire_repair_and_compression(self):
        bodies = []
        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            answer = response(('task_done', '{}')) if body.get('tools') else response(content='<step id="2">kept</step>')
            raw = provider_response(answer)
            return httpx.Response(200, headers={'content-type':'text/event-stream'},
                                  content=''.join('data: '+json.dumps(e)+'\n\n' for e in sse_events(raw)))
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            http_handler = HTTPHandler(client=client)
            original = litellm.responses
            with patch('litellm.responses', side_effect=lambda **kw: original(**kw, client=http_handler)):
                llm = subscription_llm()
                transport = SDKRawTransport(llm, policy=TraeLLMPolicy('repair','claude4-sonnet'),
                                            conformance='adapted', sleep=lambda _: None)
                initial = [{'role':'system','content':SYS_PROMPT}, {'role':'user','content':'issue'}]
                answer, reason, usage = transport(initial, TOOLS)
                self.assertEqual(reason, 'tool_calls')
                self.assertEqual(decode_wire_input(bodies[0]['instructions'], bodies[0]['input']), initial)
                compression = SDKRawTransport(llm, policy=TraeLLMPolicy('compression','gpt-5-mini-2025-08-07'),
                                              conformance='adapted', sleep=lambda _: None)
                compressor = build_compressor(llm, transport=compression, policy=compression.policy)
                result = compressor.compress_exact_result('context', step_index=2)
                self.assertEqual(result.content, 'kept')
                self.assertEqual(result.status, 'parsed')
                self.assertNotIn('cache_control', json.dumps(bodies[-1]))
                self.assertEqual(len(bodies[-1]['input']), 1)
        for body in bodies:
            self.assertTrue(body['stream'])
            self.assertFalse(body['store'])
            self.assertEqual(body['reasoning'], {'effort': 'low'})
            for key in ('temperature','max_output_tokens','n','stop','previous_response_id'):
                self.assertNotIn(key, body)

    def test_reasoning_effort_follows_config_without_reference_model_override(self):
        for effort in ('low', 'medium', None):
            with self.subTest(effort=effort):
                OpenHandsConfig(transport_conformance='adapted', reasoning_effort=effort).validate()
                llm = subscription_llm()
                llm.reasoning_effort = effort
                for role, reference in (('repair', 'claude4-sonnet'),
                                        ('compression', 'gpt-5-mini-2025-08-07')):
                    transport = SDKRawTransport(llm, policy=TraeLLMPolicy(role, reference),
                                                conformance='adapted', sleep=lambda _: None)
                    with patch('litellm.responses', return_value=provider_response(response(content='done'))) as call:
                        transport([{'role': 'system', 'content': SYS_PROMPT},
                                   {'role': 'user', 'content': 'issue'}], TOOLS if role == 'repair' else [])
                    if effort:
                        self.assertEqual(call.call_args.kwargs['reasoning'], {'effort': effort})
                    else:
                        self.assertNotIn('reasoning', call.call_args.kwargs)

    def test_audit_checks_serialized_effort_and_accepts_legacy_bodies(self):
        from openhands_adapter.compat.audit import _verify_adapted_request
        from openhands_adapter.compat.subscription_contract import capability_report
        from openhands_adapter.openhands.trae_transport import responses_payload
        instructions, items, tools = responses_payload(
            [{'role': 'system', 'content': SYS_PROMPT}, {'role': 'user', 'content': 'issue'}], TOOLS)
        for effort in ('low', None):
            metadata = {'reference_model': 'claude4-sonnet', 'wire_model': 'gpt-5.6-sol'}
            payload = {'instructions': instructions, 'input': items, 'tools': tools,
                       'stream': True, 'store': False}
            if effort:
                metadata['reasoning_effort'] = effort
                payload['reasoning'] = {'effort': effort}
            request = {'role': 'repair', 'actual_model': 'gpt-5.6-sol',
                       'reference_model': 'claude4-sonnet', 'transport_conformance': 'adapted',
                       'protocol': 'responses', 'payload': payload,
                       'capability_report': capability_report(TraeLLMPolicy('repair', 'claude4-sonnet'),
                                                             'gpt-5.6-sol', reasoning_effort=effort)}
            errors = []
            _verify_adapted_request(request, metadata, errors)
            self.assertEqual(errors, [])
            if effort:
                payload['reasoning']['effort'] = 'medium'
                _verify_adapted_request(request, metadata, errors)
                self.assertTrue(any(e['reason'] == 'adapted reasoning effort mismatch' for e in errors))

    def test_tool_only_repair_serializes_without_changing_raw_or_exact_response(self):
        from openhands_adapter.openhands.trae_transport import responses_answer
        raw = provider_response(response(('bash', ' { "command" : "pwd" } '), content=None))
        original = deepcopy(raw)
        llm = subscription_llm()
        transport = SDKRawTransport(llm, policy=TraeLLMPolicy('repair', 'claude4-sonnet'),
                                    conformance='adapted', sleep=lambda _: None)
        with patch('litellm.responses', return_value=raw):
            answer, reason, usage = transport(
                [{'role': 'system', 'content': SYS_PROMPT}, {'role': 'user', 'content': 'issue'}], TOOLS)
        self.assertEqual(answer['content'], '')
        self.assertEqual(reason, 'tool_calls')
        self.assertEqual(usage['completion_tokens'], 1)
        self.assertEqual(raw, original)
        exact_answer = responses_answer(raw)[0]
        self.assertIsNone(exact_answer['content'])
        self.assertEqual(answer['tool_calls'], exact_answer['tool_calls'])
        mgr = object.__new__(MessageManager)
        mgr.steps = [[answer]]
        trajectory = mgr.extract_step_into_traj(0)
        self.assertIn('<call tool="bash">{ "command" : "pwd" }</call>', trajectory)
        self.assertNotIn('<think>', trajectory)

    def test_retry_and_downstream_not_retried(self):
        llm = subscription_llm()
        transport = SDKRawTransport(llm, policy=TraeLLMPolicy('repair','claude4-sonnet'),
                                    conformance='adapted', sleep=lambda _: None)
        messages = [{'role':'system','content':SYS_PROMPT}, {'role':'user','content':'issue'}]
        with patch('litellm.responses', side_effect=RuntimeError('provider error')) as call:
            with self.assertRaisesRegex(RuntimeError, 'no response'): transport(messages, TOOLS)
            self.assertEqual(call.call_count, 12)
            self.assertTrue(all(c.kwargs['num_retries'] == 0 for c in call.call_args_list))
        with patch('litellm.responses', return_value=provider_response(response(content='done'))) as call, \
             patch.object(type(llm.telemetry), 'on_response', side_effect=ValueError('telemetry error')):
            with self.assertRaisesRegex(ValueError, 'telemetry error'): transport(messages, TOOLS)
            self.assertEqual(call.call_count, 1)

    def test_http_errors_have_no_hidden_retries(self):
        seen, sleeps = [], []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(500, json={'error':{'message':'fixture error','type':'server_error'}})
        llm = subscription_llm()
        transport = SDKRawTransport(llm, policy=TraeLLMPolicy('repair','claude4-sonnet'),
                                    conformance='adapted', sleep=sleeps.append)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            original = litellm.responses
            http_handler = HTTPHandler(client=client)
            with patch('litellm.responses', side_effect=lambda **kw: original(**kw, client=http_handler)):
                with self.assertRaisesRegex(RuntimeError, 'no response'):
                    transport([{'role':'system','content':SYS_PROMPT},{'role':'user','content':'issue'}], TOOLS)
        self.assertEqual(len(seen), 12)
        self.assertEqual(sleeps, [2**i for i in range(12)])
        self.assertTrue(all(body == seen[0] for body in seen))

    def test_wrapper_rejects_malformed_incomplete_and_missing_usage(self):
        llm = subscription_llm()
        transport = SDKRawTransport(llm, policy=TraeLLMPolicy('compression','gpt-5-mini-2025-08-07'),
                                    conformance='adapted', sleep=lambda _: None)
        compressor = build_compressor(llm, transport=transport, policy=transport.policy)
        for text in ('<step id="3">wrong</step>', '<step id="2"></step>',
                     '<step id="2"><step id="1">nested</step></step>', 'preamble <step id="2">x</step>',
                     '<step id="2">x</step>tail'):
            with self.subTest(text=text), patch('litellm.responses', return_value=provider_response(response(content=text))):
                self.assertEqual(compressor.compress_exact_result('ctx', step_index=2).status, 'skipped')
        raw = provider_response(response(content='<step id="2">kept</step>'))
        raw.update(status='incomplete', incomplete_details={'reason':'max_output_tokens'})
        with patch('litellm.responses', return_value=raw):
            self.assertEqual(compressor.compress_exact_result('ctx', step_index=2).reason, 'incomplete_or_nontext_compression')
        raw['usage'] = None
        with patch('litellm.responses', return_value=raw):
            self.assertEqual(compressor.compress_exact_result('ctx', step_index=2).reason, 'completion_usage_none')

    def test_bundle_uses_trae_diet_and_cannot_be_relabelled_exact(self):
        from audit_fixture import exercise
        for raw in (True, False):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as td:
                result, output, requests, compressions, _ = exercise(Path(td), raw=raw, adapted=True)
                self.assertEqual(result.agent, 'task_done')
                self.assertEqual(len(requests), 4)
                self.assertEqual(len(compressions), 1)
                report = verify_bundle(output)
                self.assertEqual(report['status'], 'partial', report)
                self.assertEqual(report['integrity_status'], 'pass')
                events = read_events(output/'events.jsonl')
                self.assertTrue(any(e['type'] == 'diet_step_change' for e in events))
                self.assertEqual({e['call_id'] for e in events if e['type'] == 'llm_call_started'},
                                 {e['request_id'] for e in events if e['type'] == 'trae_llm_request'})
                manifest_path = output/'audit/manifest.json'
                manifest = json.loads(manifest_path.read_text())
                manifest['transport_conformance'] = 'exact'
                manifest['effective_config']['openhands']['transport_conformance'] = 'exact'
                manifest['effective_config_sha256'] = payload_hash(manifest['effective_config'])
                manifest_path.write_text(json.dumps(manifest))
                self.assertEqual(verify_bundle(output)['status'], 'fail')
