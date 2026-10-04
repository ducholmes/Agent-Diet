from __future__ import annotations
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openhands_adapter.workflow.outcome import classify

class OutcomeTests(unittest.TestCase):
    def test_plausible_when_all_baseline_failures_are_fixed(self):
        outcome = classify(("A",), (), valid=True)
        self.assertEqual(outcome.status, "plausible"); self.assertEqual(outcome.fixed_test_ids, ("A",))
    def test_regression_is_negfix(self):
        self.assertEqual(classify(("A",), ("A", "B"), valid=True).status, "negfix")
    def test_unverified_evidence_is_invalid(self):
        self.assertEqual(classify(("A",), (), valid=False).status, "invalid")

if __name__ == "__main__": unittest.main()
