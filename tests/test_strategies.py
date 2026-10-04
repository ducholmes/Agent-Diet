from __future__ import annotations

import random
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.config import AgentDietConfig
from openhands_adapter.diet import strategies
from openhands_adapter.diet.condenser import AgentDietCondenser
from openhands_adapter.diet.trajectory import logical_steps


class RandomBaselineTests(unittest.TestCase):
    def test_random_matches_trae_token_deletion(self):
        encoding = tiktoken.encoding_for_model("gpt-4o")
        text = '<step id="1">\ndef f(x):\n    return x + 123456789\nLỗi 修复 🚀\n</step>'
        tokens = encoding.encode(text)
        deletable = []
        for index, token in enumerate(tokens):
            try:
                encoding.decode_single_token_bytes(token).decode("utf-8")
            except UnicodeDecodeError:
                continue
            deletable.append(index)
        for ratio in (.25, .6, 1):
            with self.subTest(ratio=ratio):
                deleted = set(random.Random(17).sample(deletable, min(int(len(tokens) * (1 - ratio)), len(deletable))))
                expected = encoding.decode([token for index, token in enumerate(tokens) if index not in deleted])
                self.assertEqual(strategies.random_drop(text, ratio=ratio, seed=17), expected)

    def test_full_retention_preserves_whitespace_exactly(self):
        text = "def f():\n    x = 1\n\treturn x\n\n"
        self.assertEqual(strategies.random_drop(text, ratio=1, seed=0), text)
        self.assertEqual(strategies.random_drop("", seed=0), "")

    def test_partial_utf8_tokens_are_never_selected_for_deletion(self):
        encoding = tiktoken.encoding_for_model("gpt-4o")
        text = "prefix 🚀 suffix"
        tokens = encoding.encode(text)
        protected = []
        for index, token in enumerate(tokens):
            try:
                encoding.decode_single_token_bytes(token).decode("utf-8")
            except UnicodeDecodeError:
                protected.append(index)
        self.assertTrue(protected, "Fixture must contain partial UTF-8 tokens")
        expected = encoding.decode([tokens[index] for index in protected])
        result = strategies.random_drop(text, ratio=0, seed=0)
        self.assertEqual(result, expected)
        self.assertIn("🚀", result)
        self.assertNotIn("\ufffd", result)

    def test_unseeded_strategy_uses_global_random_sampler(self):
        with patch("openhands_adapter.diet.strategies.random.sample", return_value=[]) as sample:
            text = "first\n    second"
            self.assertEqual(strategies.random_drop(text, ratio=.25), text)
        sample.assert_called_once()


class LinguaBaselineTests(unittest.TestCase):
    def setUp(self):
        strategies._lingua_compressor.cache_clear()

    def tearDown(self):
        strategies._lingua_compressor.cache_clear()

    def test_llmlingua2_configuration_cache_and_force_tokens(self):
        compressor = Mock()
        compressor.compress_prompt.return_value = {"compressed_prompt": "short\n?"}
        factory = Mock(return_value=compressor)
        with patch.dict(sys.modules, {"llmlingua": types.SimpleNamespace(PromptCompressor=factory)}):
            self.assertEqual(strategies.lingua("first\n?", ratio=.25), "short\n?")
            self.assertEqual(strategies.lingua("second\n?", ratio=.5), "short\n?")
        factory.assert_called_once_with(
            model_name="microsoft/llmlingua-2-xlm-roberta-large-meetingbank",
            use_llmlingua2=True, device_map="cpu",
        )
        self.assertEqual(compressor.compress_prompt.call_args_list[0].args, ("first\n?",))
        self.assertEqual(compressor.compress_prompt.call_args_list[0].kwargs, {"rate": .25, "force_tokens": ["\n", "?"]})
        self.assertEqual(compressor.compress_prompt.call_args_list[1].kwargs, {"rate": .5, "force_tokens": ["\n", "?"]})

    def test_lingua_failure_does_not_fall_back_to_random(self):
        for error in (ImportError("llmlingua unavailable"), RuntimeError("model load failed"), ValueError("bad prompt")):
            with self.subTest(error=error), patch.object(strategies, "_lingua_compressor", side_effect=error), patch.object(strategies, "random_drop") as drop:
                with self.assertRaises(type(error)):
                    strategies.lingua("original")
                drop.assert_not_called()

    def test_failed_initialization_is_not_cached(self):
        compressor = Mock()
        compressor.compress_prompt.return_value = {"compressed_prompt": "short"}
        factory = Mock(side_effect=[RuntimeError("load failed"), compressor])
        with patch.dict(sys.modules, {"llmlingua": types.SimpleNamespace(PromptCompressor=factory)}):
            with self.assertRaises(RuntimeError):
                strategies.lingua("original")
            self.assertEqual(strategies.lingua("retry"), "short")
            self.assertEqual(strategies.lingua("cached"), "short")
        self.assertEqual(factory.call_count, 2)

    def test_lingua_error_preserves_step_and_records_cause(self):
        delegate = AgentDietCondenser(AgentDietConfig(mode="lingua", threshold_tokens=1, ctx_before=0, ctx_after=0))
        with patch.object(strategies, "_lingua_compressor", side_effect=ImportError("llmlingua unavailable")), patch("openhands_adapter.diet.condenser.emit") as emit:
            result = delegate.condense([{"id": "event", "content": "original " * 30}])
        self.assertEqual(result.reason, "compressor_error")
        self.assertEqual(result.forget_event_ids, ())
        self.assertEqual(delegate.metrics["erase_count"], 0)
        emit.assert_any_call("diet_compressor_error", mode="lingua", error_type="ImportError", error="llmlingua unavailable")


class BaselineInputTests(unittest.TestCase):
    def test_baselines_receive_serialized_step(self):
        events = [{"id": "event", "content": "original " * 30}]
        serialized = logical_steps(events)[0].serialize()
        for mode, function in (("random", "random_drop"), ("lingua", "lingua")):
            with self.subTest(mode=mode), patch(f"openhands_adapter.diet.condenser.{function}", return_value="short") as strategy:
                delegate = AgentDietCondenser(AgentDietConfig(mode=mode, threshold_tokens=1, ctx_before=0, ctx_after=0, minimum_reduction_tokens=1))
                result = delegate.condense(events)
                strategy.assert_called_once_with(serialized, ratio=.25)
                self.assertEqual(result.reason, "reduced")


if __name__ == "__main__":
    unittest.main()
