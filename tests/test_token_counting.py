from __future__ import annotations

import sys
import unittest
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.diet.core import AgentDiet, count_tokens
from openhands_adapter.diet.trajectory import LogicalStep


class TokenCountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encoding = tiktoken.encoding_for_model("gpt-4o")

    def tokens(self, text):
        return len(self.encoding.encode(text, disallowed_special=()))

    def test_counts_match_trae_encoding_for_code_and_multilingual_output(self):
        for text in (
            "", "def f(x):\n    return x + 123456789\n",
            "Lỗi kiểm thử: không tìm thấy tệp. 修复测试失败 🚀",
            '<call>pytest tests/test_api.py</call>\n<result>1 failed</result>',
        ):
            with self.subTest(text=text):
                self.assertEqual(count_tokens(text), len(self.encoding.encode(text)))

    def test_special_token_spellings_in_output_are_plain_text(self):
        self.assertEqual(count_tokens("logged <|endoftext|>"), self.tokens("logged <|endoftext|>"))

    def test_threshold_and_seen_tokens_include_serialized_step_wrapper(self):
        step = LogicalStep(12, ("event",), "tests passed")
        serialized_tokens = self.tokens(step.serialize())
        self.assertGreater(serialized_tokens, self.tokens(step.text))
        diet = AgentDiet(threshold_tokens=serialized_tokens, ctx_before=0, ctx_after=0)
        candidate = diet.candidate((step,))
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.input_tokens, serialized_tokens)
        self.assertEqual(diet.metrics.seen_tokens, serialized_tokens)
        self.assertEqual(candidate.context, step.serialize())
        diet = AgentDiet(threshold_tokens=serialized_tokens + 1, ctx_before=0, ctx_after=0)
        self.assertIsNone(diet.candidate((step,)))
        self.assertEqual(diet.metrics.rejected, {"below_threshold": 1})

    def test_absolute_reduction_boundary_and_metrics_match_trae(self):
        step = LogicalStep(0, ("event",), "verbose output " * 100)
        replacement = "tests passed"
        before, after = self.tokens(step.serialize()), self.tokens(replacement)
        saved = before - after
        for minimum, expected in ((saved, True), (saved + 1, False)):
            with self.subTest(minimum=minimum):
                diet = AgentDiet(threshold_tokens=1, ctx_before=0, ctx_after=0,
                                 minimum_reduction_tokens=minimum, minimum_reduction_ratio=.999)
                candidate = diet.candidate((step,))
                self.assertEqual(diet.accept(candidate, replacement), expected)
                self.assertEqual(diet.metrics.erase_in_tokens, before if expected else 0)
                self.assertEqual(diet.metrics.erase_out_tokens, after if expected else 0)

    def test_ratio_reduction_boundary_uses_serialized_input(self):
        step = LogicalStep(0, ("event",), "verbose output " * 100)
        replacement = "short output " * 20
        actual_reduction = 1 - self.tokens(replacement) / self.tokens(step.serialize())
        for ratio, expected in ((actual_reduction - .001, True), (actual_reduction + .001, False)):
            with self.subTest(ratio=ratio):
                diet = AgentDiet(threshold_tokens=1, ctx_before=0, ctx_after=0,
                                 minimum_reduction_tokens=10000, minimum_reduction_ratio=ratio)
                self.assertEqual(diet.accept(diet.candidate((step,)), replacement), expected)


if __name__ == "__main__":
    unittest.main()
