"""Trae parity and native SDK history checks, without provider network calls."""
from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from openhands.sdk.agent.utils import prepare_llm_messages
from openhands.sdk.context.view import View
from openhands.sdk.event import ActionEvent, AgentErrorEvent, MessageEvent, ObservationEvent
from openhands.sdk.event.condenser import Condensation
from openhands.sdk.llm import Message, MessageToolCall, TextContent
from openhands.sdk.tool.builtins.think import ThinkObservation
from openhands_adapter.config import AgentDietConfig
from openhands_adapter.diet.condenser import AgentDietCondenser
from openhands_adapter.diet.trajectory import logical_steps
from openhands_adapter.openhands.condenser import build_sdk_condenser


def action(eid, response, name='shell', text='', arguments=None, reasoning=None, origin="completion"):
    arguments = arguments or {'command': eid}
    return ActionEvent(
        id=eid, llm_response_id=response, tool_name=name, tool_call_id='call-' + eid,
        tool_call=MessageToolCall(id='call-' + eid, name=name, arguments=json.dumps(arguments), origin=origin),
        action=None, thought=[TextContent(text=text)] if text else [],
        reasoning_content=reasoning,
    )


def observation(a, output):
    return ObservationEvent(id='o-' + a.id, action_id=a.id, tool_name=a.tool_name,
                            tool_call_id=a.tool_call_id,
                            observation=ThinkObservation.from_text(text=output))


def assistant(eid, text):
    return MessageEvent(id=eid, source='agent', llm_response_id='response-' + eid,
                        llm_message=Message(role='assistant', content=[TextContent(text=text)]))


def user():
    return MessageEvent(id='user', source='user',
                        llm_message=Message(role='user', content=[TextContent(text='Fix parser.')]))


def trae_serializer():
    # Load only the actual serializer class; importing expert starts provider setup.
    path = Path(__file__).resolve().parents[1] / 'artifact/artifact/code/trae_agent/agents/expert.py'
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'MessageManager')
    namespace = {}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['MessageManager'].__new__(namespace['MessageManager'])


class TurnTrajectoryTests(unittest.TestCase):
    def test_chat_and_responses_tool_calls_have_identical_trajectory(self):
        serialized = []
        for origin in ('completion', 'responses'):
            a = action('a', 'response', text='Read file.', origin=origin,
                       reasoning='EXCLUDED REASONING')
            b = action('b', 'response', origin=origin)
            if origin == 'responses':
                a = a.model_copy(update={'tool_call': a.tool_call.model_copy(
                    update={'responses_item_id': 'fc-response-item'})})
            steps = logical_steps([user(), a, b, observation(a, 'file'), observation(b, 'tests')])
            self.assertEqual(len(steps), 2)
            self.assertTrue(steps[1].complete)
            serialized.append(steps[1].serialize())
            self.assertNotIn('EXCLUDED REASONING', serialized[-1])
            self.assertEqual(a.tool_call.origin, origin)
            if origin == 'responses':
                self.assertEqual(a.tool_call.responses_item_id, 'fc-response-item')
        self.assertEqual(serialized[0], serialized[1])

    def test_system_and_unowned_events_do_not_increment_turn_count(self):
        steps = logical_steps([
            {'id': 'system', 'type': 'system_prompt', 'content': 'system'},
            user(), {'id': 'other', 'type': 'unknown', 'content': 'other'},
            {'id': 'orphan', 'type': 'observation', 'tool_call_id': 'unknown', 'content': 'orphan'},
            assistant('first', 'first'), assistant('second', 'second'),
        ])
        self.assertEqual([step.index for step in steps], [-1, 0, 1])
        self.assertEqual([step.event_ids for step in steps], [('user',), ('first',), ('second',)])

    def test_native_sdk_serialization_matches_original_trae(self):
        a = action('a', 'response', text='Read and think.', reasoning='EXCLUDED REASONING')
        b = action('b', 'response', name='think', arguments={'thought': 'Check parser.'})
        oa, ob = observation(a, 'file output'), observation(b, 'Your thought has been logged.')
        oa = oa.model_copy(update={'extended_content': [TextContent(text=' extra context')]})
        steps = logical_steps([user(), a, b, oa, ob])
        mgr = trae_serializer()
        mgr.user_message = {'content': 'Fix parser.'}
        mgr.steps = [[
            {'role': 'assistant', 'content': 'Read and think.', 'reasoning_content': 'EXCLUDED REASONING',
             'tool_calls': [{'function': {'name': c.tool_name, 'arguments': c.tool_call.arguments}}
                            for c in (a, b)]},
            {'role': 'tool', 'content': 'file output extra context', 'agent_caller': ('shell', {})},
            {'role': 'tool', 'content': 'Continue.', 'agent_caller': ('think', {'thought': 'Check parser.'})},
        ]]
        self.assertEqual([s.index for s in steps], [-1, 0])
        self.assertEqual(steps[0].serialize(), mgr.extract_step_into_traj(-1))
        self.assertEqual(steps[1].serialize(), mgr.extract_step_into_traj(0))
        self.assertEqual(steps[1].event_ids, ('a', 'b', 'o-a', 'o-b'))
        self.assertTrue(steps[1].complete)
        self.assertEqual(a.reasoning_content, 'EXCLUDED REASONING')

    def test_incomplete_batch_cannot_be_compressed_even_with_zero_future_context(self):
        a, b = action('a', 'response'), action('b', 'response')
        delegate = AgentDietCondenser(AgentDietConfig(mode='delete', threshold_tokens=1, ctx_before=0, ctx_after=0))
        self.assertEqual(delegate.condense([a, b, observation(a, 'output')]).reason, 'no_candidate')
        result = delegate.condense([a, b, observation(a, 'output'), observation(b, 'output')])
        self.assertEqual(result.forget_event_ids, ('a', 'b', 'o-a', 'o-b'))
        self.assertEqual(delegate.original_steps[0].index, 0)

    def test_reordered_results_and_tool_errors_belong_to_the_same_response(self):
        a, b = action('a', 'response'), action('b', 'response')
        error = AgentErrorEvent(id='err-a', tool_name=a.tool_name, tool_call_id=a.tool_call_id,
                                error='Invalid arguments')
        steps = logical_steps([a, b, observation(b, 'output b'), error])
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0].complete)
        self.assertIn('<result>Invalid arguments</result>', steps[0].text)
        self.assertEqual(steps[0].event_ids, ('a', 'b', 'o-b', 'err-a'))

    def test_failed_think_call_keeps_the_error_as_a_result(self):
        a = action('a', 'response', name='ThinkTool', arguments={'thought': 'Plan.'})
        error = AgentErrorEvent(id='error', tool_name=a.tool_name, tool_call_id=a.tool_call_id,
                                error='ThinkTool execution failed')
        step = logical_steps([a, error])[0]
        self.assertTrue(step.complete)
        self.assertIn('<result>ThinkTool execution failed</result>', step.text)
        self.assertNotIn('<think>', step.text)

    def test_legacy_events_without_ids_still_pair(self):
        step = logical_steps([{'type': 'action', 'action': 'run'},
                              {'type': 'observation', 'observation': 'output'}])[0]
        self.assertTrue(step.complete)
        self.assertEqual(step.event_ids, ('0', '1'))

    def test_user_prompt_is_context_and_never_a_target(self):
        delegate = AgentDietCondenser(AgentDietConfig(mode='delete', threshold_tokens=1, ctx_before=1, ctx_after=2))
        self.assertEqual(delegate.condense([user()]).reason, 'no_candidate')
        events = [user(), assistant('first', 'first'), assistant('second', 'second'), assistant('third', 'third')]
        result = delegate.condense(events)
        self.assertEqual(result.forget_event_ids, ('first',))
        self.assertEqual(sorted(delegate.original_steps), [-1, 0, 1, 2])
        candidate = delegate.diet.candidate(delegate._steps_for_compression(events))
        self.assertTrue(candidate.context.startswith('Fix parser.\n<step id="0">'))
        zero = AgentDietCondenser(AgentDietConfig(mode='delete', threshold_tokens=1, ctx_before=0, ctx_after=0))
        self.assertEqual(zero.condense([user()]).reason, 'no_candidate')

    def test_sdk_forgets_entire_batch_without_additional_property_removals(self):
        compressor = Mock(return_value='short')
        delegate = AgentDietCondenser(AgentDietConfig(mode='ours', threshold_tokens=1, ctx_before=1, ctx_after=1,
                                                     minimum_reduction_tokens=1), compressor=compressor)
        sdk = build_sdk_condenser(delegate)
        a, b = action('a', 'response', text='original ' * 30), action('b', 'response')
        events = [user(), a, b, observation(a, 'output a'), observation(b, 'output b'), assistant('next', 'next')]
        result = sdk.condense(View(events=events))
        self.assertIsInstance(result, Condensation)
        self.assertEqual(result.forgotten_event_ids, {'a', 'b', 'o-a', 'o-b'})
        projected = View.from_events([*events, result])
        self.assertEqual([e.id for e in projected.events], ['user', result.summary_event.id, 'next'])
        sdk.condense(projected)
        self.assertEqual(compressor.call_count, 1)
        later = assistant('later', 'later ' * 30)
        result2 = sdk.condense(View(events=[*projected.events, later]))
        self.assertEqual(result2.forgotten_event_ids, {'next'})
        self.assertEqual(delegate.summary_to_step[result.summary_event.id], 0)
        self.assertEqual(delegate.summary_to_step[result2.summary_event.id], 1)
        self.assertIn('original ', compressor.call_args.args[1])
        ready = View.from_events([*events, result, later, result2])
        messages = prepare_llm_messages(ready, condenser=sdk)
        self.assertFalse(any(m.tool_calls for m in messages))
        self.assertFalse(any(m.role == 'tool' for m in messages))


if __name__ == '__main__':
    unittest.main()
