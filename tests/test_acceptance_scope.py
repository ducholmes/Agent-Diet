from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.config import AgentDietConfig
from openhands_adapter.diet.condenser import AgentDietCondenser
from openhands_adapter.diet.core import count_tokens
from openhands_adapter.diet.trajectory import logical_steps


class AcceptanceScopeTests(unittest.TestCase):
    events = [{"id": "target", "content": "original output " * 30}]

    def condenser(self, mode, compressor=None, **kwargs):
        return AgentDietCondenser(AgentDietConfig(
            mode=mode, threshold_tokens=1, ctx_before=0, ctx_after=0,
            minimum_reduction_tokens=10000, minimum_reduction_ratio=.99, **kwargs,
        ), compressor=compressor)

    def test_baselines_accept_output_even_when_reduction_gate_would_reject(self):
        original = logical_steps(self.events)[0].serialize()
        for mode, function in (("random", "random_drop"), ("lingua", "lingua")):
            for replacement in (original, original * 2):
                with self.subTest(mode=mode, replacement=replacement), patch(
                    f"openhands_adapter.diet.condenser.{function}", return_value=replacement,
                ):
                    delegate = self.condenser(mode)
                    result = delegate.condense(self.events)
                    self.assertEqual(result.reason, "reduced")
                    self.assertEqual(result.summary, replacement)
                    self.assertEqual(result.forget_event_ids, ("target",))
                    self.assertEqual(delegate.metrics["erase_count"], 1)
                    self.assertEqual(delegate.metrics["erase_in_tokens"], count_tokens(original))
                    self.assertEqual(delegate.metrics["erase_out_tokens"], count_tokens(replacement))
                    self.assertNotIn("insufficient_reduction", delegate.metrics["rejected"])

    def test_ours_still_rejects_insufficient_reduction(self):
        original = logical_steps(self.events)[0].serialize()
        delegate = self.condenser("ours", compressor=lambda text, context: original)
        result = delegate.condense(self.events)
        self.assertEqual(result.reason, "rejected")
        self.assertEqual(result.forget_event_ids, ())
        self.assertEqual(delegate.metrics["erase_count"], 0)
        self.assertEqual(delegate.metrics["rejected"], {"insufficient_reduction": 1})

    def test_delete_records_zero_output_tokens(self):
        delegate = self.condenser("delete")
        result = delegate.condense(self.events)
        self.assertEqual(result.reason, "reduced")
        self.assertEqual(result.summary, "")
        self.assertEqual(delegate.metrics["erase_count"], 1)
        self.assertEqual(delegate.metrics["erase_out_tokens"], 0)

    def test_baselines_still_obey_candidate_threshold_and_lz4(self):
        for mode, function in (("delete", "delete"), ("random", "random_drop"), ("lingua", "lingua")):
            for gate in ("threshold", "lz4"):
                with self.subTest(mode=mode, gate=gate):
                    delegate = self.condenser(mode, use_lz4=gate == "lz4")
                    if gate == "threshold":
                        delegate.diet.threshold_tokens = 10000
                    with patch(f"openhands_adapter.diet.condenser.{function}") as strategy, patch.object(
                        delegate.diet, "_compressible", return_value=0,
                    ):
                        result = delegate.condense(self.events)
                    self.assertEqual(result.reason, "no_candidate")
                    strategy.assert_not_called()
                    self.assertEqual(delegate.metrics["erase_count"], 0)

    def test_invalid_baseline_output_still_preserves_step(self):
        for mode, function in (("random", "random_drop"), ("lingua", "lingua")):
            with self.subTest(mode=mode), patch(f"openhands_adapter.diet.condenser.{function}", return_value=None):
                delegate = self.condenser(mode)
                result = delegate.condense(self.events)
                self.assertEqual(result.reason, "invalid_compressor_output")
                self.assertEqual(result.forget_event_ids, ())
                self.assertEqual(delegate.metrics["erase_count"], 0)


if __name__ == "__main__":
    unittest.main()
