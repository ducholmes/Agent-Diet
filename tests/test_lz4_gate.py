from __future__ import annotations

import math
import random
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import lz4.frame

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.diet.core import AgentDiet, count_tokens
from openhands_adapter.diet.trajectory import LogicalStep


def trae_saved_tokens(target, following):
    raw = target.encode("utf-8")
    x1 = len(lz4.frame.compress(following.encode("utf-8")))
    x2 = len(lz4.frame.compress((target + following).encode("utf-8")))
    return count_tokens(target) * (1 - max(0, x2 - x1) / len(raw))


class LZ4GateTests(unittest.TestCase):
    def test_duplicate_future_output_changes_gate_decision(self):
        target = random.Random(17).randbytes(2048).hex()
        steps = (LogicalStep(0, ("target",), target), LogicalStep(1, ("future",), target))
        different = (steps[0], LogicalStep(1, ("future",), random.Random(18).randbytes(2048).hex()))
        duplicate_score = trae_saved_tokens(steps[0].serialize(), steps[1].serialize())
        different_score = trae_saved_tokens(different[0].serialize(), different[1].serialize())
        self.assertGreater(duplicate_score, different_score)
        threshold = math.ceil((duplicate_score + different_score) / 2)
        diet = AgentDiet(threshold_tokens=threshold, ctx_before=0, ctx_after=1, use_lz4=True)
        self.assertIsNotNone(diet.candidate(steps))
        self.assertIsNone(diet.candidate(different))
        self.assertEqual(diet.metrics.rejected, {"lz4_not_compressible": 1})
        ungated = AgentDiet(threshold_tokens=threshold, ctx_before=0, ctx_after=1)
        self.assertIsNotNone(ungated.candidate(different))

    def test_gate_matches_trae_formula_at_threshold_for_utf8_context(self):
        steps = tuple(LogicalStep(i, (str(i),), text) for i, text in enumerate((
            "before", "Lỗi kiểm thử 修复 🚀 " * 50, "Lỗi kiểm thử 修复 🚀 " * 50, "tests passed",
        )))
        target = steps[1].serialize()
        following = "".join(step.serialize() for step in steps[2:])
        expected = trae_saved_tokens(target, following)
        for threshold, accepted in ((math.floor(expected), True), (math.floor(expected) + 1, False)):
            with self.subTest(threshold=threshold):
                diet = AgentDiet(threshold_tokens=threshold, ctx_before=1, ctx_after=2, use_lz4=True)
                candidate = diet.candidate(steps)
                self.assertEqual(candidate is not None, accepted)
                if candidate:
                    self.assertEqual(candidate.step.event_ids, ("1",))

    def test_only_target_and_future_context_are_compressed(self):
        steps = tuple(LogicalStep(i, (str(i),), text) for i, text in enumerate((
            "preceding", "target", "future A", "future B",
        )))
        diet = AgentDiet(threshold_tokens=0, ctx_before=1, ctx_after=2, use_lz4=True)
        with patch("lz4.frame.compress", wraps=lz4.frame.compress) as compress:
            diet.candidate(steps)
        future = "".join(step.serialize() for step in steps[2:])
        self.assertEqual([call.args[0] for call in compress.call_args_list], [
            future.encode("utf-8"), (steps[1].serialize() + future).encode("utf-8"),
        ])

    def test_zero_future_context_compresses_empty_baseline(self):
        steps = (LogicalStep(0, ("before",), "preceding"), LogicalStep(1, ("target",), "repeat " * 50))
        expected = trae_saved_tokens(steps[1].serialize(), "")
        diet = AgentDiet(threshold_tokens=math.floor(expected), ctx_before=1, ctx_after=0, use_lz4=True)
        with patch("lz4.frame.compress", wraps=lz4.frame.compress) as compress:
            self.assertIsNotNone(diet.candidate(steps))
        self.assertEqual([call.args[0] for call in compress.call_args_list], [
            b"", steps[1].serialize().encode("utf-8"),
        ])

    def test_negative_marginal_size_is_clamped_and_threshold_equality_passes(self):
        step = LogicalStep(0, ("target",), "target")
        steps = (step, LogicalStep(1, ("future",), "future"))
        tokens = count_tokens(step.serialize())
        diet = AgentDiet(threshold_tokens=tokens, ctx_before=0, ctx_after=1, use_lz4=True)
        with patch("lz4.frame.compress", side_effect=[b"x" * 100, b"x" * 80]):
            self.assertIsNotNone(diet.candidate(steps))

    def test_below_token_threshold_skips_lz4(self):
        step = LogicalStep(0, ("target",), "target")
        diet = AgentDiet(threshold_tokens=count_tokens(step.serialize()) + 1,
                         ctx_before=0, ctx_after=0, use_lz4=True)
        with patch("lz4.frame.compress") as compress:
            self.assertIsNone(diet.candidate((step,)))
        compress.assert_not_called()


if __name__ == "__main__":
    unittest.main()
