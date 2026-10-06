import asyncio
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
import litellm
from openai import OpenAI
from openhands.sdk import LLM
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands_adapter.compat.trae_contract import TOOLS, SYS_PROMPT, PROFILES
from openhands_adapter.openhands.trae_agent import TraeContractAgent, TraeLocalConversation
from openhands_adapter.openhands.trae_transport import SDKRawTransport
from openhands_adapter.openhands.prompts import build_user_prompt
from trae_reference import assert_reference_hashes, response, run_expert
from test_trae_contract_turns import ScriptedTransport, Sandbox
from trae_transport_fixture import CAPABILITIES


class SDKTests(unittest.TestCase):
    def setUp(self): assert_reference_hashes()

    def test_serialized_http_schema_and_raw_arguments(self):
        seen = []
        arguments = ' { "x" : invalid '
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={
                'id': 'raw', 'object': 'chat.completion', 'model': 'gpt-4o',
                'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                    'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call', 'type': 'function',
                        'function': {'name': 'unknown', 'arguments': arguments}}]}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3},
            })
        client = OpenAI(api_key='fake', base_url='https://mock.invalid/v1',
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        llm = LLM(model='openai/gpt-4o', api_key='fake', base_url='https://mock.invalid/v1', reasoning_effort=None)
        transport = SDKRawTransport(llm)
        messages = [{'role': 'system', 'content': SYS_PROMPT}, {'role': 'user', 'content': 'issue\n'}]
        with patch('openhands.sdk.llm.llm.litellm_completion', side_effect=lambda **kw: litellm.completion(**kw, client=client)):
            answer, _, usage = transport(messages, TOOLS)
        client.close()
        self.assertEqual(seen[0]['messages'], messages)
        self.assertEqual(seen[0]['tools'], TOOLS)
        self.assertEqual(answer['tool_calls'][0]['function']['arguments'], arguments)
        self.assertEqual(answer['tool_calls'][0]['function']['name'], 'unknown')
        self.assertEqual(usage['completion_tokens'], 2)

    def test_exact_assembly_and_secret_context_do_not_change_requests(self):
        from openhands_adapter.config import OpenHandsConfig, AgentDietConfig, WorkflowConfig
        from openhands_adapter.openhands.agent import build_agent
        from openhands_adapter.openhands.container import RepairContainer
        from openhands.sdk.secret import StaticSecret
        llm=LLM(model='openai/test',api_key='fake')
        with tempfile.TemporaryDirectory() as td:
            with patch('openhands_adapter.openhands.agent.build_llm',return_value=llm), \
                 patch('openhands_adapter.openhands.trae_session.stage_tools'), \
                 patch('openhands_adapter.openhands.trae_session.TraeSandbox',return_value=Sandbox()):
                agent,condenser=build_agent(RepairContainer('fake',Path(td),'image'),
                    OpenHandsConfig(auth='api-key', trae_capabilities=CAPABILITIES),AgentDietConfig(mode='skip'),WorkflowConfig(),execution_plan={})
            transport=ScriptedTransport([response(('task_done','{}'))])
            agent._transport=transport
            c=TraeLocalConversation(agent=agent,workspace=td,visualizer=None,stuck_detection=False,max_iteration_per_run=51)
            try:
                c.update_secrets({'TOKEN':StaticSecret(value='fake',description='FORBIDDEN SECRET DESCRIPTION')})
                c.send_message(build_user_prompt(Path('/testbed'),problem_statement='issue'))
                c.run()
                self.assertEqual(transport.requests[0][0][0],{'role':'system','content':SYS_PROMPT})
                self.assertNotIn('FORBIDDEN',json.dumps(transport.requests))
                self.assertEqual(agent.include_default_tools,[])
                self.assertEqual(agent.tools,[])
                self.assertIsNone(agent.agent_context)
            finally:c.close()

    def test_subscription_uses_responses_transport(self):
        llm = LLM(model='openai/test', api_key='fake'); llm.is_subscription = True
        self.assertEqual(SDKRawTransport(llm).protocol, 'responses')

    def test_async_terminal_and_both_cap_profiles_cleanup(self):
        async def exercise(profile, script, patches):
            with tempfile.TemporaryDirectory() as td:
                sandbox = Sandbox(patches=patches)
                transport = ScriptedTransport(script)
                hooks = []
                agent = TraeContractAgent(llm=LLM(model='openai/test', api_key='fake'), reference_profile=profile).bind_runtime(
                    transport=transport, sandbox=sandbox, after_normal_turn=lambda mgr: hooks.append(mgr.count_turn()))
                c = TraeLocalConversation(agent=agent, workspace=td, visualizer=None, stuck_detection=False,
                                          max_iteration_per_run=PROFILES[profile][0]+1)
                try:
                    c.send_message(build_user_prompt(Path('/testbed'), problem_statement='Lỗi\n  preserved\ttext'))
                    await c.arun()
                    self.assertEqual(c.state.execution_status, ConversationExecutionStatus.FINISHED)
                    snapshot = deepcopy(c.state.agent_state['trae_contract'])
                    with redirect_stdout(StringIO()):
                        expected = run_expert(script, Sandbox(patches=patches), profile=profile)
                    self.assertEqual((transport.requests, snapshot['steps'], snapshot['gen'], snapshot['patch']), expected)
                    self.assertEqual(hooks, [] if snapshot['gen']=='task_done' else list(range(1, PROFILES[profile][0]+1)))
                finally: c.close()
                self.assertEqual(sandbox.closed, 1)
        for profile, (cap, _) in PROFILES.items():
            asyncio.run(exercise(profile, [response(('task_done',' { } '))], ['diff --git a/a b/a\n']))
            asyncio.run(exercise(profile, [response(content='again') for _ in range(cap)], ['WIP\n']))

    def test_persisted_contract_is_only_request_history_and_resume_keeps_raw_arguments(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); archive = root/'archive'
            transport = ScriptedTransport([response(('think',' { "thought" : "raw" } ')), response(('task_done','{}'))])
            def make():
                agent = TraeContractAgent(llm=LLM(model='openai/test', api_key='fake')).bind_runtime(transport=transport,sandbox=Sandbox())
                return TraeLocalConversation(agent=agent,workspace=root,persistence_dir=archive,visualizer=None,stuck_detection=False,max_iteration_per_run=51,delete_on_close=False)
            c = make(); c.send_message(build_user_prompt(Path('/testbed'), problem_statement='issue'))
            cid = c.state.id
            with c.state: c.agent.step(c,c._on_event)
            stored = deepcopy(c.state.agent_state['trae_contract']); c.close()
            agent = TraeContractAgent(llm=LLM(model='openai/test',api_key='fake')).bind_runtime(transport=transport,sandbox=Sandbox())
            resumed = TraeLocalConversation(agent=agent,workspace=root,persistence_dir=archive,conversation_id=cid,
                visualizer=None,stuck_detection=False,max_iteration_per_run=51,delete_on_close=False)
            try:
                self.assertEqual(resumed.state.agent_state['trae_contract']['steps'][0][0], stored['steps'][0][0])
                resumed.run()
                self.assertEqual(len(transport.requests),2)
                self.assertEqual(transport.requests[1][0][2]['tool_calls'][0]['function']['arguments'], ' { "thought" : "raw" } ')
                self.assertFalse(resumed.agent.dynamic_context)
                self.assertEqual(resumed.agent.tools_map, {})
                self.assertIsNone(resumed._hook_processor)
            finally: resumed.close()
