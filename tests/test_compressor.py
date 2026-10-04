from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.config import AgentDietConfig, OpenHandsConfig, WorkflowConfig
from openhands_adapter.openhands.agent import build_agent
from openhands_adapter.openhands.compressor import build_compressor
from openhands_adapter.openhands.container import RepairContainer
from openhands_adapter.openhands.worker import run_worker


class TextContent:
    def __init__(self, *, text):
        self.text = text


class Message:
    def __init__(self, *, role, content, tool_calls=None):
        self.role, self.content, self.tool_calls = role, content, tool_calls


class Agent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class CompressorTests(unittest.TestCase):
    def setUp(self):
        self.sdk = types.SimpleNamespace(
            Message=Message, TextContent=TextContent, Agent=Agent,
            AgentContext=types.SimpleNamespace,
        )
        self.modules = {"openhands": types.ModuleType("openhands"), "openhands.sdk": self.sdk}
        self.llm = Mock()
        self.llm.uses_responses_api.return_value = False
        self.llm.disable_stop_word = False
        self.llm._model_features.return_value = types.SimpleNamespace(supports_stop_words=True)
        self.llm.completion.return_value = types.SimpleNamespace(
            message=Message(role="assistant", content=[TextContent(text='<step id="0">short summary</step>')]),
            raw_response={"usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                          "choices": [{"finish_reason": "stop"}]},
        )

    def test_completion_receives_target_and_context_and_returns_only_text(self):
        self.llm.completion.return_value.message.content = [
            TextContent(text='<step id="0">short'), object(), TextContent(text=" summary</step>"),
        ]
        with patch.dict(sys.modules, self.modules):
            result = build_compressor(self.llm)("original step", "nearby steps")
        self.assertEqual(result, "short summary")
        kwargs = self.llm.completion.call_args.kwargs
        self.assertIsNone(kwargs["tools"])
        self.assertEqual([m.role for m in kwargs["messages"]], ["system", "user"])
        user_prompt = kwargs["messages"][1].content[0].text
        self.assertIn('original step', user_prompt)
        self.assertIn('nearby steps', user_prompt)
        self.assertIn('Now, compress the step 0.', user_prompt)
        self.assertEqual(kwargs['stop'], '</step>')

    def test_empty_or_tool_call_response_is_rejected(self):
        for content, calls in [([], None), ([TextContent(text="   ")], None),
                               ([TextContent(text="summary")], ["tool"])]:
            with self.subTest(content=content, calls=calls), patch.dict(sys.modules, self.modules):
                self.llm.completion.return_value.message = Message(
                    role="assistant", content=content, tool_calls=calls,
                )
                with self.assertRaises(ValueError):
                    build_compressor(self.llm)("original", "context")

    def test_agent_selects_compressor_model_and_preserves_injection(self):
        injected = lambda text, context: "injected"
        cases = [
            (AgentDietConfig(), None, 1, True),
            (AgentDietConfig(compressor_model="other-model"), None, 2, True),
            (AgentDietConfig(), injected, 1, True),
            (AgentDietConfig(enabled=False), None, 1, False),
            *[(AgentDietConfig(mode=mode), None, 1, False)
              for mode in ("skip", "delete", "random", "lingua")],
        ]
        for diet, override, llm_count, available in cases:
            with self.subTest(diet=diet, override=override), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with (
                    patch.dict(sys.modules, self.modules),
                    patch("openhands_adapter.openhands.agent.require_pinned_sdk"),
                    patch("openhands_adapter.openhands.agent.build_sdk_condenser", side_effect=lambda d: d),
                    patch("openhands_adapter.openhands.agent.workspace_tool_specs"),
                    patch("openhands_adapter.openhands.agent.build_llm", return_value=self.llm) as factory,
                ):
                    _, delegate = build_agent(
                        RepairContainer("repair", root, "image"), OpenHandsConfig(),
                        diet, WorkflowConfig(), execution_plan={phase: [] for phase in ("setup", "build", "target_test", "regression_test")}, compressor=override,
                    )
                    self.assertEqual(factory.call_count, llm_count)
                    self.assertEqual(delegate.compressor is not None, available)
                    if llm_count == 2:
                        self.assertEqual(factory.call_args.kwargs, {"model": "other-model"})
                    if override is not None:
                        self.assertIs(delegate.compressor, override)

    def test_worker_ours_calls_llm_and_reduces_candidate_without_injection(self):
        events = [{"id": "old-step", "type": "message", "content": "long " * 50}]
        results = []

        class Conversation:
            def __init__(self, *, agent, **kwargs):
                self.agent = agent

            def send_message(self, prompt):
                pass

            def run(self):
                results.append(self.agent.condenser.condense(events))

            def close(self):
                pass

        self.sdk.LocalConversation = Conversation
        self.llm.metrics = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.dict(sys.modules, self.modules),
                patch("openhands_adapter.openhands.worker.require_pinned_sdk"),
                patch("openhands_adapter.openhands.worker.disable_ambient_discovery"),
                patch("openhands_adapter.openhands.worker.configure"),
                patch("openhands_adapter.openhands.agent.require_pinned_sdk"),
                patch("openhands_adapter.openhands.agent.build_sdk_condenser", side_effect=lambda d: d),
                patch("openhands_adapter.openhands.agent.workspace_tool_specs"),
                patch("openhands_adapter.openhands.agent.build_llm", return_value=self.llm),
            ):
                with patch('openhands_adapter.openhands.worker.emit') as emit:
                    run_worker(
                        root, "repair", root, container=RepairContainer("repair", root, "image"),
                        openhands=OpenHandsConfig(),
                        execution_plan={phase: [] for phase in ("setup", "build", "target_test", "regression_test")}, diet=AgentDietConfig(threshold_tokens=3, ctx_before=0, ctx_after=0,
                                             minimum_reduction_tokens=1), workflow=WorkflowConfig(),
                    )
                metrics = next(call.kwargs['metrics'] for call in emit.call_args_list if call.args[0] == 'diet_metrics')
                self.assertEqual(metrics['analysis_count'], 1)
                self.assertEqual(metrics['analysis_prompt_tokens'], 100)
                self.assertEqual(metrics['analysis_completion_tokens'], 10)
                self.assertEqual(metrics['compression_total_tokens'], 110)
        self.llm.completion.assert_called_once()
        self.assertEqual(results[0].reason, "reduced")
        self.assertEqual(results[0].forget_event_ids, ("old-step",))
        self.assertEqual(results[0].summary, "short summary")

    def test_response_api_uses_full_wrapper_without_stop_or_prefill(self):
        self.llm.uses_responses_api.return_value = True
        reply = self.llm.completion.return_value
        reply.raw_response = {"usage": {"input_tokens": 80, "output_tokens": 12, "total_tokens": 92}, "status": "completed"}
        self.llm.responses.return_value = reply
        usage = Mock()
        with patch.dict(sys.modules, self.modules):
            self.assertEqual(build_compressor(self.llm, on_usage=usage)("original", "context"), "short summary")
        self.llm.completion.assert_not_called()
        self.assertNotIn("stop", self.llm.responses.call_args.kwargs)
        self.assertEqual([m.role for m in self.llm.responses.call_args.kwargs['messages']], ['system', 'user'])
        usage.assert_called_once_with({"prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92})

    def test_explicit_prefill_and_stop_handle_removed_closing_tag(self):
        reply = self.llm.completion.return_value
        reply.message.content = [TextContent(text="short summary")]
        with patch.dict(sys.modules, self.modules):
            self.assertEqual(build_compressor(self.llm, assistant_prefill=True)("original", "context"), "short summary")
        self.assertEqual(self.llm.completion.call_args.kwargs['messages'][-1].role, 'assistant')
        self.assertIn('<step id="0">', self.llm.completion.call_args.kwargs['messages'][-1].content[0].text)

    def test_protocol_rejects_truncation_missing_wrapper_or_wrong_id_but_counts_usage(self):
        reply = self.llm.completion.return_value
        for output, reason in [
            ('<step id="0">short</step>', 'length'),
            ('<step id="0">short</step>', 'content_filter'),
            ('<step id="0">short</step>', 'error'),
            ('<step id="0">short', 'length'),
            ('<step id="1">short</step>', 'stop'),
            ('short</step>', 'stop'),
            ('<step id="0"><step id="0">short</step>', 'stop'),
        ]:
            with self.subTest(output=output, reason=reason), patch.dict(sys.modules, self.modules):
                reply.message.content = [TextContent(text=output)]
                reply.raw_response['choices'] = [{'finish_reason': reason}]
                usage = Mock()
                with self.assertRaises(ValueError):
                    build_compressor(self.llm, on_usage=usage)("original", "context")
                usage.assert_called_once()

    def test_explicit_step_id_and_code_indentation_are_preserved(self):
        reply = self.llm.completion.return_value
        reply.message.content = [TextContent(text='<step id="42">\n    return value\n</step>extra')]
        with patch.dict(sys.modules, self.modules):
            compressor = build_compressor(self.llm)
            result = compressor.compress_step('original', '<step id="42">original</step>', step_index=42)
        self.assertEqual(result, '\n    return value\n')
        self.assertIn('Now, compress the step 42.', self.llm.completion.call_args.kwargs['messages'][1].content[0].text)

    def test_missing_usage_and_incomplete_responses_are_rejected(self):
        reply = self.llm.completion.return_value
        with patch.dict(sys.modules, self.modules):
            reply.raw_response = {'choices': [{'finish_reason': 'stop'}]}
            with self.assertRaisesRegex(ValueError, 'missing token usage'):
                build_compressor(self.llm)('original', 'context')
            self.llm.uses_responses_api.return_value = True
            reply.raw_response = {'usage': {'input_tokens': 2, 'output_tokens': 3}, 'status': 'incomplete'}
            self.llm.responses.return_value = reply
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                build_compressor(self.llm)('original', 'context')

    def test_llm_failure_leaves_candidate_unchanged(self):
        from openhands_adapter.diet.condenser import AgentDietCondenser

        self.llm.completion.side_effect = RuntimeError("provider unavailable")
        delegate = AgentDietCondenser(
            AgentDietConfig(threshold_tokens=1, ctx_before=0, ctx_after=0),
            compressor=build_compressor(self.llm),
        )
        with patch.dict(sys.modules, self.modules):
            result = delegate.condense([{"id": "step", "content": "long " * 50}])
        self.assertEqual(result.reason, "compressor_error")
        self.assertEqual(result.forget_event_ids, ())


if __name__ == "__main__":
    unittest.main()
