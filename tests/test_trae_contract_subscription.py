"""Subscription wire and SDK lifecycle checks against the frozen Trae oracle."""
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from openhands.sdk import LLM
from openhands.sdk.llm.auth.credentials import OAuthCredentials
from openhands.sdk.llm.auth.openai import OpenAISubscriptionAuth

from openhands_adapter.compat.trae_contract import TOOLS, SYS_PROMPT, PROFILES
from openhands_adapter.openhands.prompts import build_user_prompt
from openhands_adapter.openhands.trae_agent import TraeContractAgent, TraeLocalConversation
from openhands_adapter.openhands.trae_transport import SDKRawTransport, responses_payload, responses_answer
from trae_reference import assert_reference_hashes, response, run_expert
from test_trae_contract_turns import Sandbox


def subscription_llm():
    creds = OAuthCredentials(vendor='openai', access_token='synthetic-oauth-token',
        refresh_token='fake-refresh', expires_at=int(time.time()*1000)+3_600_000)
    return OpenAISubscriptionAuth().create_llm(model='gpt-5.6-sol', credentials=creds,
                                             reasoning_effort='low')


def provider_response(scripted):
    answer, reason, usage = scripted
    output = []
    if answer.get('content') is not None:
        output.append({'type': 'message', 'id': 'msg-test', 'role': 'assistant', 'status': 'completed',
                       'content': [{'type': 'output_text', 'text': answer['content'], 'annotations': []}]})
    for call in answer.get('tool_calls') or []:
        output.append({'type': 'function_call', 'id': f"fc-{call['id']}", 'call_id': call['id'],
                       'name': call['function']['name'], 'arguments': call['function']['arguments'], 'status': 'completed'})
    return {'id': 'resp-test', 'created_at': 1, 'model': 'gpt-5.6-sol', 'object': 'response',
            'status': 'completed', 'output': output,
            'usage': None if usage['completion_tokens'] is None else {
                'input_tokens': 3, 'output_tokens': usage['completion_tokens'],
                'total_tokens': 3+usage['completion_tokens']}}


def sse_events(raw, *, empty_final=True):
    for index, item in enumerate(raw['output']):
        yield {'type': 'response.output_item.done', 'output_index': index, 'item': deepcopy(item),
               'sequence_number': index}
    final = deepcopy(raw)
    if empty_final:
        final['output'] = []
    yield {'type': 'response.completed', 'response': final, 'sequence_number': len(raw['output'])}


def decode_wire_input(instructions, items):
    """Independent inverse for comparing emitted Responses history to the oracle."""
    messages = [{'role': 'system', 'content': instructions}]
    for item in items:
        if item.get('type') == 'function_call':
            if messages[-1]['role'] != 'assistant' or any(
                call['id'] == item['call_id'] for call in messages[-1].get('tool_calls', [])
            ):
                messages.append({'role': 'assistant', 'content': None})
            messages[-1].setdefault('tool_calls', []).append({'type': 'function', 'id': item['call_id'],
                'function': {'name': item['name'], 'arguments': item['arguments']}})
        elif item.get('type') == 'function_call_output':
            messages.append({'role': 'tool', 'tool_call_id': item['call_id'], 'content': item['output']})
        else:
            messages.append({'role': item['role'], 'content': item['content']})
    return messages


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        assert_reference_hashes()
        # Synthetic OAuth identity only: do not fetch real JWKS in offline tests.
        identity = patch('openhands.sdk.llm.auth.openai._extract_chatgpt_account_id', return_value='test-account')
        identity.start()
        self.addCleanup(identity.stop)

    def test_http_serialization_stream_raw_arguments_and_history(self):
        seen = []
        bad = ' { "x" : invalid '
        first = response(('unknown', bad), ('think', ' { "thought" : "ý\\n" } '), content='  text\n')
        scripted = iter([first, response(('task_done', '{}'))])
        def handler(request):
            seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
            raw = provider_response(next(scripted))
            body = ''.join('data: '+json.dumps(event)+'\n\n' for event in sse_events(raw))
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)
        llm = subscription_llm()
        client = httpx.Client(transport=httpx.MockTransport(handler))
        http_handler = HTTPHandler(client=client)
        messages = [{'role': 'system', 'content': SYS_PROMPT}, {'role': 'user', 'content': 'issue\n  lỗi\t'}]
        initial = deepcopy(messages)
        transport = SDKRawTransport(llm)
        original_responses = litellm.responses
        with patch('litellm.responses', side_effect=lambda **kw: original_responses(**kw, client=http_handler)):
            answer, reason, usage = transport(messages, TOOLS)
            self.assertEqual(answer, first[0])
            self.assertEqual(reason, first[1])
            self.assertEqual(usage['completion_tokens'], 1)
            self.assertEqual(llm.metrics.accumulated_token_usage.completion_tokens, 1)
            messages.append(answer)
            messages.extend([{'role': 'tool', 'tool_call_id': 'call-0', 'content': 'Invalid JSON\n'},
                             {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'Continue.'}])
            transport(messages, TOOLS)
        client.close()
        for url, headers, body in seen:
            self.assertEqual(url, llm.base_url+'/responses')
            self.assertEqual(body['instructions'], SYS_PROMPT)
            self.assertTrue(body['stream'])
            self.assertFalse(body['store'])
            self.assertEqual(headers['chatgpt-account-id'], 'test-account')
            self.assertEqual(headers['authorization'], 'Bearer '+llm._subscription_credentials.access_token)
            self.assertEqual(headers['openai-beta'], 'responses=experimental')
            for expected, actual in zip(TOOLS, body['tools'], strict=True):
                self.assertEqual(actual, {'type': 'function', **expected['function'], 'strict': False})
            self.assertNotIn('temperature', body)
            self.assertNotIn('max_output_tokens', body)
            self.assertNotIn('previous_response_id', body)
            self.assertNotIn('Context (system prompt):', json.dumps(body))
        self.assertEqual(decode_wire_input(seen[0][2]['instructions'], seen[0][2]['input']), initial)
        self.assertEqual(decode_wire_input(seen[1][2]['instructions'], seen[1][2]['input']), messages)
        self.assertEqual(seen[1][2]['input'][2]['arguments'], bad)

    def test_exact_subscription_assembly_rejects_before_login_or_tool_staging(self):
        from openhands_adapter.config import OpenHandsConfig, AgentDietConfig, WorkflowConfig
        from openhands_adapter.openhands.agent import build_agent
        from openhands_adapter.openhands.container import RepairContainer
        with tempfile.TemporaryDirectory() as td, \
             patch('openhands_adapter.openhands.agent.build_llm') as build, \
             patch('openhands_adapter.openhands.trae_session.stage_tools') as stage:
            with self.assertRaisesRegex(ValueError, 'repair: subscription'):
                build_agent(RepairContainer('fake', Path(td), 'image'),
                    OpenHandsConfig(auth='subscription'), AgentDietConfig(mode='skip'), WorkflowConfig(), execution_plan={})
            build.assert_not_called()
            stage.assert_not_called()

    def test_sdk_refresh_and_contract_fields_override_extra_body(self):
        llm = subscription_llm()
        fresh = llm._subscription_credentials.model_copy(update={'access_token': 'refreshed-synthetic-token'})
        llm._subscription_credentials = fresh.model_copy(update={'expires_at': 1})
        llm.litellm_extra_body = {'instructions': 'FORBIDDEN', 'input': 'FORBIDDEN', 'tools': [],
                                 'stream': False, 'store': True, 'previous_response_id': 'FORBIDDEN'}
        messages = [{'role': 'system', 'content': SYS_PROMPT}, {'role': 'user', 'content': '  issue\n'}]
        with patch('openhands.sdk.llm.auth.openai.OpenAISubscriptionAuth.refresh_if_needed_sync', return_value=fresh) as refresh, \
             patch('litellm.responses', return_value=provider_response(response(('think', '{}')))) as call:
            _, _, usage = SDKRawTransport(llm)(messages, TOOLS)
        refresh.assert_called_once()
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs['api_key'], fresh.access_token)
        self.assertEqual(kwargs['instructions'], SYS_PROMPT)
        self.assertEqual(kwargs['input'], [{'role': 'user', 'content': '  issue\n'}])
        self.assertEqual(kwargs['extra_headers']['chatgpt-account-id'], 'test-account')
        self.assertTrue(kwargs['stream'])
        self.assertFalse(kwargs['store'])
        self.assertNotIn('FORBIDDEN', json.dumps(kwargs))
        self.assertEqual(usage['completion_tokens'], 1)

    def test_terminal_empty_patch_batches_missing_usage_and_caps_match_oracle(self):
        async def exercise(profile, script, patches):
            sandbox = Sandbox(patches=patches)
            hooks, requests = [], []
            raw_script = iter(script)
            def respond(**kw):
                requests.append((decode_wire_input(kw['instructions'], kw['input']), deepcopy(TOOLS)))
                return iter(sse_events(provider_response(next(raw_script))))
            llm = subscription_llm()
            agent = TraeContractAgent(llm=llm, reference_profile=profile).bind_runtime(
                transport=SDKRawTransport(llm), sandbox=sandbox,
                after_normal_turn=lambda mgr: hooks.append(mgr.count_turn()))
            with tempfile.TemporaryDirectory() as td, patch('litellm.responses', side_effect=respond):
                conversation = TraeLocalConversation(agent=agent, workspace=td, visualizer=None,
                    stuck_detection=False, max_iteration_per_run=PROFILES[profile][0]+1)
                try:
                    conversation.send_message(build_user_prompt(Path('/testbed'), problem_statement='Lỗi\n  preserved\ttext'))
                    await conversation.arun()
                    snapshot = deepcopy(conversation.state.agent_state['trae_contract'])
                    self.assertEqual(conversation.state.execution_status.value, 'finished')
                finally:
                    conversation.close()
            expected_hooks = []
            expected = run_expert(script, Sandbox(patches=patches), profile=profile,
                                 hook=lambda mgr: expected_hooks.append(mgr.count_turn()))
            # is_error is internal dispatcher metadata, outside Responses' item
            # schema; the exact error text and logical state must survive.
            expected_requests = deepcopy(expected[0])
            for messages, _ in expected_requests:
                for message in messages:
                    message.pop('is_error', None)
            self.assertEqual(requests, expected_requests)
            self.assertEqual((snapshot['steps'], snapshot['gen'], snapshot['patch']), expected[1:])
            self.assertEqual(hooks, expected_hooks)
            self.assertEqual(sandbox.closed, 1)
        for profile, (cap, _) in PROFILES.items():
            scripts = [
                ([response(('task_done', '{}'))], ['WIP\n']),
                ([response(content='  text\n'), response(('task_done', '{}'))], ['WIP\n']),
                ([response(('task_done', '{}')), response(('task_done', '{}'))], ['', 'WIP\n']),
                ([response(('task_done', '{}'), usage=None), response(('think', 'bad JSON')),
                  response(('unknown', '{}')), response(('task_done', '{}'), ('think', '{}')),
                  response(('task_done', '{}'))], ['WIP\n']),
                ([response(('task_failed', '{}'))], ['']),
                ([response(('think', ' { "thought" : "repeat" } ')) for _ in range(cap)], ['WIP\n']),
            ]
            for script, patches in scripts:
                with self.subTest(profile=profile, length=len(script)), redirect_stdout(StringIO()):
                    asyncio.run(exercise(profile, script, patches))

    def test_stream_failures_close_and_do_not_return_partial_calls(self):
        class Stream:
            def __init__(self, events): self.events, self.closed = events, False
            def __iter__(self): return iter(self.events)
            def close(self): self.closed = True
        raw = provider_response(response(('task_done', '{}')))
        partial = list(sse_events(raw))[:-1]
        for tail in [[], [{'type': 'response.failed', 'response': {'status': 'failed'}}],
                     [{'type': 'error', 'message': 'failed'}],
                     [{'type': 'response.completed', 'response': {**raw, 'status': 'failed'}}]]:
            stream = Stream(partial+tail)
            with self.subTest(tail=tail), patch('litellm.responses', return_value=stream):
                with self.assertRaisesRegex(RuntimeError, 'Subscription Responses'):
                    SDKRawTransport(subscription_llm())([{'role': 'system', 'content': SYS_PROMPT},
                                                        {'role': 'user', 'content': 'issue'}], TOOLS)
                self.assertTrue(stream.closed)

    def test_incomplete_usage_and_projection_preserve_optional_schema(self):
        raw = provider_response(response(('bash', ' { invalid ')))
        raw.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
        answer, reason, usage = responses_answer(raw)
        self.assertEqual(reason, 'length')
        self.assertEqual(answer['tool_calls'][0]['function']['arguments'], ' { invalid ')
        raw['usage'] = None
        self.assertIsNone(responses_answer(raw)[2]['completion_tokens'])
        messages = [{'role': 'system', 'content': '  exact\n'}, {'role': 'user', 'content': '  issue\n'}]
        original = deepcopy((messages, TOOLS))
        instructions, items, tools = responses_payload(messages, TOOLS)
        self.assertEqual(instructions, '  exact\n')
        self.assertEqual(items, [{'role': 'user', 'content': '  issue\n'}])
        self.assertEqual((messages, TOOLS), original)
        self.assertEqual(tools[0]['parameters']['required'], ['command', 'path'])
        self.assertNotIn('required', tools[1]['parameters'])
        self.assertNotIn('additionalProperties', tools[0]['parameters'])
