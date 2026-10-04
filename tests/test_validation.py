from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openhands_adapter.input_loader import CommandSpec
from openhands_adapter.workflow.command import run_command
class ValidationCommandTests(unittest.TestCase):
    def test_command_captures_exit_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_command(CommandSpec(("/bin/sh", "-c", "printf ok")), Path(directory), timeout_seconds=5)
        self.assertTrue(result.succeeded); self.assertEqual(result.stdout, "ok")
    def test_command_timeout_is_a_result(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_command(CommandSpec(("/bin/sh", "-c", "sleep 1")), Path(directory), timeout_seconds=1)
        self.assertTrue(result.timed_out)
if __name__ == "__main__": unittest.main()
