from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands.sdk.context.view import View
from openhands.sdk.agent.utils import prepare_llm_messages
from openhands.sdk.event import Event, MessageEvent
from openhands.sdk.event.condenser import Condensation, CondensationSummaryEvent
from openhands.sdk.llm import LLM, LLMResponse, Message, TextContent
from openhands.sdk.llm.utils.metrics import MetricsSnapshot, TokenUsage
from litellm import ModelResponse

from openhands_adapter.config import AgentDietConfig
from openhands_adapter.diet.condenser import AgentDietCondenser
from openhands_adapter.openhands.condenser import build_sdk_condenser
from openhands_adapter.openhands.compressor import build_compressor


def message(event_id, text):
    return MessageEvent(id=event_id, source="agent", llm_message=Message(
        role="assistant", content=[TextContent(text=text)],
    ))


def config(mode="ours", **kwargs):
    return AgentDietConfig(mode=mode, threshold_tokens=1, minimum_reduction_tokens=1, **kwargs)


class SDKHistoryTests(unittest.TestCase):
    def test_real_sdk_response_uses_request_usage_instead_of_accumulated_metrics(self):
        llm = LLM(model="openai/gpt-4o", api_key="test-no-network", api_mode="chat")
        raw = ModelResponse(
            id="response-1", choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": '<step id="7">short</step>'}}],
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        )
        response = LLMResponse(
            message=Message(role="assistant", content=[TextContent(text='<step id="7">short</step>')]),
            raw_response=raw,
            metrics=MetricsSnapshot(accumulated_token_usage=TokenUsage(prompt_tokens=9000, completion_tokens=800)),
        )
        usage = Mock()
        with patch.object(LLM, "completion", return_value=response) as request:
            result = build_compressor(llm, on_usage=usage).compress_step("original", '<step id="7">original</step>', step_index=7)
        self.assertEqual(result, "short")
        usage.assert_called_once_with({"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
        self.assertTrue(all(isinstance(m, Message) for m in request.call_args.kwargs["messages"]))

    def test_delete_keeps_assistant_reminder_at_original_position(self):
        delegate = AgentDietCondenser(config("delete", ctx_before=1, ctx_after=1))
        sdk = build_sdk_condenser(delegate)
        events = [message("before", "before"), message("target", "original " * 30), message("after", "after")]
        view = View(events=events)
        result = sdk.condense(view)
        self.assertIsInstance(result, Condensation)
        self.assertEqual(result.forgotten_event_ids, {"target"})
        self.assertEqual(result.summary, "(System reminder: long content deleted for better efficiency)")
        self.assertEqual(result.summary_offset, 1)
        # Persist using SDK-native event serialization, then apply again.
        restored = Event.model_validate_json(result.model_dump_json())
        self.assertIsInstance(restored, Condensation)
        projected = View.from_events([*events, restored])
        self.assertIsInstance(projected.events[1], CondensationSummaryEvent)
        agent_view = sdk.condense(projected)
        self.assertIsInstance(agent_view, View)
        self.assertEqual([e.id for e in agent_view.events], ["before", result.summary_event.id, "after"])
        reminder = agent_view.events[1].to_llm_message()
        self.assertEqual(reminder.role, "assistant")
        self.assertEqual(reminder.content[0].text, result.summary)
        self.assertEqual(view.events, events)
        # Cached SDK view was not mutated to an assistant event.
        self.assertEqual(projected.events[1].to_llm_message().role, "user")
        self.assertEqual(delegate.original_steps[1].text, "<think>" + "original " * 30 + "</think>")
        self.assertEqual(delegate.metrics["erase_count"], 1)
        messages = prepare_llm_messages(projected, condenser=sdk)
        self.assertEqual(messages[1].role, "assistant")
        self.assertEqual(messages[1].content[0].text, result.summary)
        self.assertFalse(any("original" in c.text for m in messages for c in m.content))

    def test_successive_compressions_use_original_context_and_stable_indices(self):
        compressor = Mock(return_value="short")
        delegate = AgentDietCondenser(config(ctx_before=1, ctx_after=1), compressor=compressor)
        sdk = build_sdk_condenser(delegate)
        before = message("before", "before")
        first = message("first", "FIRST ORIGINAL " * 30)
        second = message("second", "SECOND ORIGINAL " * 30)
        events = [before, first, second]
        result1 = sdk.condense(View(events=events))
        self.assertEqual(result1.summary, "(System reminder: compressed for better efficiency) short")
        projected = result1.apply(events)
        # Immediate retries supply an LLM view rather than recompressing summary.
        ready = sdk.condense(View(events=projected))
        self.assertIsInstance(ready, View)
        self.assertEqual(compressor.call_count, 1)
        later = message("later", "later")
        result2 = sdk.condense(View(events=[*projected, later]))
        self.assertEqual(result2.forgotten_event_ids, {"second"})
        target, context = compressor.call_args.args
        self.assertEqual(target, "<think>" + "SECOND ORIGINAL " * 30 + "</think>")
        self.assertIn('<step id="1">\n<think>FIRST ORIGINAL', context)
        self.assertIn('<step id="2">\n<think>SECOND ORIGINAL', context)
        self.assertIn('<step id="3">\n<think>later', context)
        self.assertNotIn("System reminder", context)
        self.assertEqual(delegate.summary_to_step[result1.summary_event.id], 1)
        self.assertEqual(delegate.summary_to_step[result2.summary_event.id], 2)
        agent_view = sdk.condense(View(events=result2.apply([*projected, later])))
        all_text = " ".join(c.text for e in agent_view.events for c in e.to_llm_message().content)
        self.assertNotIn("FIRST ORIGINAL", all_text)
        self.assertNotIn("SECOND ORIGINAL", all_text)
        self.assertEqual(compressor.call_count, 2)

    def test_foreign_sdk_summary_keeps_its_role(self):
        delegate = AgentDietCondenser(AgentDietConfig(mode="skip"))
        sdk = build_sdk_condenser(delegate)
        foreign = CondensationSummaryEvent(summary="external summary")
        ready = sdk.condense(View(events=[foreign]))
        self.assertIs(ready.events[0], foreign)
        self.assertEqual(ready.events[0].to_llm_message().role, "user")


class OriginalStepTests(unittest.TestCase):
    def test_parallel_action_observation_pairs_keep_originals_and_summary_mapping(self):
        delegate = AgentDietCondenser(config("delete", ctx_before=0, ctx_after=1))
        events = [
            {"id": "a", "type": "action", "tool_call_id": "a", "action": "run a"},
            {"id": "b", "type": "action", "tool_call_id": "b", "action": "run b"},
            {"id": "oa", "type": "observation", "tool_call_id": "a", "observation": "original a"},
            {"id": "ob", "type": "observation", "tool_call_id": "b", "observation": "original b"},
        ]
        result = delegate.condense(events)
        self.assertEqual(result.forget_event_ids, ("a", "oa"))
        delegate.bind_summary("summary-a", result.forget_event_ids)
        new_view = [{"id": "summary-a", "content": "reminder"}, events[1], events[3]]
        steps = delegate._steps_for_compression(new_view)
        self.assertEqual([s.index for s in steps], [0, 1])
        self.assertEqual(steps[0].event_ids, ("summary-a",))
        self.assertIn("original a", steps[0].text)
        self.assertEqual(steps[1].event_ids, ("b", "ob"))
        self.assertEqual(delegate.condense(new_view).reason, "already_compressed")

    def test_unmatched_action_is_updated_when_observation_arrives(self):
        delegate = AgentDietCondenser(config(ctx_before=0, ctx_after=0))
        action = {"id": "a", "type": "action", "tool_call_id": "call", "action": "run"}
        observation = {"id": "o", "type": "observation", "tool_call_id": "call", "observation": "output"}
        delegate._steps_for_compression([action])
        steps = delegate._steps_for_compression([action, observation])
        self.assertEqual(steps[0].index, 0)
        self.assertEqual(delegate.original_steps[0].event_ids, ("a", "o"))
        self.assertIn("output", delegate.original_steps[0].text)


if __name__ == "__main__":
    unittest.main()
