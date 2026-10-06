from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import httpx
from openhands.sdk import LLM
from openhands_adapter.compat.trae_contract import MessageManager, TOOLS
from openhands_adapter.compat.trae_llm_policy import TraeLLMPolicy
from openhands_adapter.config import OpenHandsConfig, AgentDietConfig, WorkflowConfig
from openhands_adapter.diet.condenser import AgentDietCondenser
from openhands_adapter.diet.prompts import SYSTEM_PROMPT, build_compression_messages, build_compression_window
from openhands_adapter.events import configure, read_events
from openhands_adapter.openhands.agent import build_agent
from openhands_adapter.openhands.compressor import build_compressor
from openhands_adapter.openhands.container import RepairContainer
from openhands_adapter.openhands.trae_agent import TraeLocalConversation
from openhands_adapter.token_tracking import aggregate_calls
from trae_reference import compression_oracle, wrapper_oracle
from trae_transport_fixture import CAPABILITIES, capture_transport, completion
from test_trae_contract_turns import Sandbox


def manager():
    mgr = MessageManager('/testbed', 'literal <think> issue; think agent', None, {}, True, 50, TOOLS)
    for i in range(3):
        arguments = '{"thought": "think agent in arguments"}'
        mgr.push_step({'role': 'assistant', 'content': 'think agent in code', 'tool_calls': [
            {'id': f'call-{i}', 'type': 'function', 'function': {'name': 'think', 'arguments': arguments}}]},
            [{'role': 'tool', 'content': 'Continue.', 'tool_call_id': f'call-{i}',
              'agent_caller': ('think', {'thought': 'think agent in arguments'})}])
    return mgr


class CompressorPayloadTests(unittest.TestCase):
    def tearDown(self): configure(None)

    def test_exact_messages_and_wire_match_source_for_both_branches(self):
        for reference in ('claude4-sonnet', 'gpt-5-mini-2025-08-07'):
            for show_ctx in (True, False):
                with self.subTest(reference=reference, show_ctx=show_ctx):
                    mgr = manager()
                    base, (model, expected_messages, tools, kwargs) = compression_oracle(mgr, reference, show_ctx=show_ctx)
                    self.assertEqual(SYSTEM_PROMPT, base)
                    policy = TraeLLMPolicy('compression', reference)
                    context = build_compression_window(mgr, 0, policy, ctx_before=1, ctx_after=2, show_ctx=show_ctx)
                    self.assertEqual(build_compression_messages(context, 0, policy), expected_messages)
                    if show_ctx:
                        self.assertIn('literal <think> issue', context)
                        self.assertIn('<call tool="think">{"thought": "think agent in arguments"}</call>', context)
                    else:
                        self.assertEqual(context, '\n'+mgr.extract_step_into_traj(0, policy.bypass_filter)+'\n\n')
                    expected_wire = []
                    wrapper_oracle(lambda **kw: expected_wire.append(kw) or type('Result', (), {'model_dump': lambda s: {}})(), lambda n: None)(model, expected_messages, tools, kwargs)
                    expected_wire[0]['model'] = 'gpt-5.6-sol'
                    with capture_transport('compression', reference) as (transport, seen, sleeps, clients):
                        usages = []
                        compressor = build_compressor(transport.llm, transport=transport, policy=policy, on_usage=usages.append)
                        self.assertEqual(compressor.compress_step('unused', context, step_index=0), 'short')
                    self.assertEqual(seen, expected_wire)
                    self.assertEqual([{k:u[k] for k in ('prompt_tokens','completion_tokens','total_tokens')} for u in usages], [{'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}])

    def test_prefill_cannot_be_disabled_in_exact_mode(self):
        with capture_transport('compression', 'gpt-5-mini-test') as (transport, *_):
            with self.assertRaisesRegex(ValueError, 'prefill'):
                build_compressor(transport.llm, policy=transport.policy, transport=transport, assistant_prefill=False)

    def test_repair_cache_placement_survives_serializer(self):
        mgr = manager()
        first = MessageManager('/testbed', 'issue', None, {}, True, 50, TOOLS).format_messages()
        tool_ending = mgr.format_messages()
        mgr.push_step({'role': 'assistant', 'content': 'continue'}, [])
        assistant_ending = mgr.format_messages()
        for messages in (first, tool_ending, assistant_ending):
            with capture_transport('repair', 'claude4-sonnet') as (transport, seen, *_):
                transport(deepcopy(messages), TOOLS)
            self.assertEqual(seen[0]['messages'], messages)
        self.assertNotIn('cache_control', json.dumps(first))
        self.assertEqual(tool_ending[-2]['content'][-1]['cache_control'], {'type': 'ephemeral'})
        self.assertNotIn('cache_control', json.dumps(tool_ending[-1]))
        self.assertNotIn('cache_control', json.dumps(assistant_ending[-2:]))

    def test_hook_sends_bypass_window_but_gate_uses_original_tags(self):
        for show_ctx in (True, False):
            mgr = manager()
            mgr.steps[0][0]['content'] *= 100
            policy = TraeLLMPolicy('compression', 'gpt-5-mini-test')
            class Compressor:
                def compress_step(self, text, context, *, step_index):
                    self.seen = text, context, step_index
                    return 'short'
            compressor = Compressor()
            condenser = AgentDietCondenser(AgentDietConfig(threshold_tokens=0, show_ctx=show_ctx,
                minimum_reduction_tokens=400, minimum_reduction_ratio=.20), compressor=compressor, compression_policy=policy, exact_trae=True)
            expected_mgr = deepcopy(mgr)
            condenser.after_normal_turn(mgr)
            self.assertIn('<talk>', mgr.steps[0][0]['agent_erased'])
            _, (_, messages, _, _) = compression_oracle(expected_mgr, policy.reference_model, show_ctx=show_ctx)
            self.assertEqual(compressor.seen[1]+'\n\nNow, compress the step 0.', messages[1]['content'])
            self.assertEqual(condenser.diet.metrics.analysis_count, 1)
            self.assertEqual(condenser.diet.metrics.erase_count, 1)

    def test_original_recovery_keeps_source_nested_window_shape(self):
        mgr = manager()
        original = mgr.extract_step_into_traj(1)
        mgr.perform_erase_step(1, 'summary', original)
        policy = TraeLLMPolicy('compression', 'gpt-5-mini-test')
        context = build_compression_window(mgr, 0, policy, ctx_before=1, ctx_after=2, show_ctx=True)
        _, (_, expected, _, _) = compression_oracle(mgr, policy.reference_model)
        self.assertEqual(build_compression_messages(context, 0, policy), expected)
        self.assertIn('<step id="1">\n'+original+'\n</step>', context)

    def test_repair_compression_repair_integration_shared_actual_llm(self):
        repair_calls = 0
        def handler(request):
            nonlocal repair_calls
            body = json.loads(request.content)
            if 'tools' not in body:
                return httpx.Response(200, json=completion('short</step>'))
            repair_calls += 1
            name = 'task_done' if repair_calls == 4 else 'think'
            args = '{}' if name == 'task_done' else '{"thought":"think agent literal"}'
            calls = [{'id': f'call-{repair_calls}', 'type': 'function', 'function': {'name': name, 'arguments': args}}]
            return httpx.Response(200, json=completion(('think agent code line\n'*400) if repair_calls == 1 else '', calls=calls, finish='tool_calls'))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            configure(root/'events.jsonl')
            shared = LLM(model='openai/gpt-5.6-sol', api_key='fake', base_url='https://mock.invalid/v1')
            with capture_transport('repair', 'claude4-sonnet', llm=shared, handler=handler) as (_, seen, _, _):
                with patch('openhands_adapter.openhands.agent.build_llm', return_value=shared) as factory, \
                     patch('openhands_adapter.openhands.trae_session.stage_tools'), \
                     patch('openhands_adapter.openhands.trae_session.TraeSandbox', return_value=Sandbox()):
                    agent, condenser = build_agent(RepairContainer('fake', root, 'image'),
                        OpenHandsConfig(auth='api-key', model=shared.model, trae_capabilities=CAPABILITIES),
                        AgentDietConfig.from_mapping({'compressor_model': 'inherit'}), WorkflowConfig(), execution_plan={})
                self.assertEqual(factory.call_count, 1)
                conversation = TraeLocalConversation(agent=agent, workspace=root, visualizer=None,
                    stuck_detection=False, max_iteration_per_run=51)
                try:
                    conversation.send_message('literal <think> initial issue')
                    conversation.run()
                    snapshot = conversation.state.agent_state['trae_contract']
                finally:
                    conversation.close()
            self.assertEqual(snapshot['turns'], 4)
            self.assertEqual(snapshot['gen'], 'task_done')
            self.assertEqual([('repair' if 'tools' in body else 'compression') for body in seen],
                             ['repair', 'repair', 'repair', 'compression', 'repair'])
            self.assertIn('compressed for better efficiency) short', seen[-1]['messages'][2]['content'])
            self.assertEqual(seen[3]['reasoning_effort'], 'low')
            self.assertNotIn('temperature', seen[3])
            for body in (seen[0], seen[1], seen[2], seen[4]):
                self.assertNotIn('reasoning_effort', body)
                self.assertEqual(body['temperature'], 0.0)
            metrics = condenser.metrics
            self.assertEqual(metrics['analysis_count'], 1)
            self.assertEqual(metrics['erase_count'], 1)
            self.assertEqual(metrics['analysis_prompt_tokens'], 100)
            self.assertEqual(metrics['analysis_completion_tokens'], 10)
            usage = aggregate_calls(read_events(root/'events.jsonl'))
            self.assertEqual(usage['repair']['calls'], 4)
            self.assertEqual(usage['compression']['calls'], 1)
            self.assertEqual(usage['total']['total_tokens'], 550)
