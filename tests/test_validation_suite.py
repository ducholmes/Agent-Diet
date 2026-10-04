from __future__ import annotations
import sys, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openhands_adapter.input_loader import CaseSpec, CommandSpec, EnvironmentSpec
from openhands_adapter.workflow.models import CommandResult
from openhands_adapter.workflow.validation import run_post_patch

def case() -> CaseSpec:
    root = Path("/tmp")
    return CaseSpec("case", "case", root, root, root / "c.json", root / "f.log", 6, EnvironmentSpec("image", "docker", "image"), (), (), (CommandSpec(("test", "{test_id}"), evidence_pattern="PASS|FAIL", failure_pattern="FAIL"),), (CommandSpec(("test", "--all"), evidence_pattern="PASS|FAIL", failure_pattern="FAIL"),), ("broken",), (), True, True)

class ValidationSuiteTests(unittest.TestCase):
    def test_validation_checks_evidence_and_failures_beyond_agent_output_limit(self):
        from types import SimpleNamespace
        for marker, expected in (("PASS", "plausible"), ("FAIL", "negfix")):
            with self.subTest(marker=marker):
                completed = SimpleNamespace(returncode=0, stdout="x" * 50_000 + "\n" + marker, stderr="")
                with patch("openhands_adapter.workflow.validation.ensure_available"), patch("openhands_adapter.workflow.command.docker_command", return_value=(("test",), Path("/tmp"))), patch("openhands_adapter.workflow.command.subprocess.run", return_value=completed):
                    result = run_post_patch(case(), Path("/tmp"), timeout_seconds=5)
                self.assertEqual(result.status, expected)
                self.assertTrue(result.target_valid)
                self.assertTrue(result.regression_valid)
                self.assertEqual(result.commands[-1].stdout, completed.stdout)

    def test_target_expands_and_regression_always_runs(self):
        results = [
            CommandResult(("test", "broken"), ".", 0, .1, "PASS", ""),
            CommandResult(("test", "--all"), ".", 0, .1, "PASS", ""),
        ]
        with patch("openhands_adapter.workflow.validation.ensure_available"), patch("openhands_adapter.workflow.validation.run_command", side_effect=results) as run:
            result = run_post_patch(case(), Path("/tmp"), timeout_seconds=5, output_limit_bytes=100)
        self.assertEqual(run.call_count, 2); self.assertEqual(result.status, "plausible"); self.assertTrue(result.passed)

    def test_zero_tests_is_invalid(self):
        results = [CommandResult(("test", "broken"), ".", 0, .1, "no tests were found", "")]
        with patch("openhands_adapter.workflow.validation.ensure_available"), patch("openhands_adapter.workflow.validation.run_command", side_effect=results):
            result = run_post_patch(case(), Path("/tmp"), timeout_seconds=5, output_limit_bytes=100)
        self.assertEqual(result.status, "invalid"); self.assertEqual(result.reason, "target_test_invalid")

    def test_failing_target_without_execution_evidence_is_invalid(self):
        results = [CommandResult(("test", "broken"), ".", 1, .1, "", "")]
        with patch("openhands_adapter.workflow.validation.ensure_available"), patch("openhands_adapter.workflow.validation.run_command", side_effect=results):
            result = run_post_patch(case(), Path("/tmp"), timeout_seconds=5, output_limit_bytes=100)
        self.assertEqual(result.status, "invalid")

if __name__ == "__main__": unittest.main()
