from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openhands_adapter.openhands.container import RepairContainer
from openhands_adapter.openhands.workspace_tool import WorkspaceTools
from openhands_adapter.workflow.patch import create_baseline


def plan():
    return {
        "setup": [], "build": [],
        "target_test": [{"argv": ["bash", "-lc", "pytest tests/test_parser.py -k 'empty input'"], "cwd": "build"}],
        "regression_test": [{"argv": ["pytest", "tests/test_parser.py"], "cwd": "."}],
    }


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "build").mkdir()
        self.execution_plan = plan()
        self.tools = WorkspaceTools(RepairContainer("repair", self.root, "image"),
                                    execution_plan=self.execution_plan,
                                    timeout_seconds=3, output_limit_bytes=4000)

    def test_command_preserves_exact_argv_and_cwd_via_docker(self):
        with patch("openhands_adapter.openhands.workspace_tool.subprocess.run") as run:
            run.return_value.stdout, run.return_value.stderr, run.return_value.returncode = "passed", "", 0
            result = self.tools.run_configured_command("target_test", 0)
        self.assertEqual(run.call_args.args[0], (
            "docker", "exec", "--workdir", str(self.root / "build"), "repair", "timeout", "3",
            "bash", "-lc", "pytest tests/test_parser.py -k 'empty input'"))
        self.assertEqual(result.exit_code, 0)

    def test_unknown_commands_and_negative_or_noninteger_indices_never_execute(self):
        with patch("openhands_adapter.openhands.workspace_tool.subprocess.run") as run:
            for phase, index in (("target_test", -1), ("target_test", 1), ("setup", 0),
                                 ("shell", 0), ("target_test", True), ("target_test", "0")):
                with self.subTest(phase=phase, index=index):
                    self.assertEqual(self.tools.run_configured_command(phase, index).exit_code, 126)
        run.assert_not_called()

    def test_plan_cannot_be_changed_by_caller_or_workspace_config(self):
        self.execution_plan["target_test"][0]["argv"][:] = ["pytest", "tests/"]
        (self.root / ".agent-diet.repair-config.json").write_text(json.dumps(self.execution_plan))
        listed = json.loads(self.tools.list_configured_commands())
        self.assertEqual(listed["target_test"][0]["argv"], plan()["target_test"][0]["argv"])
        with patch("openhands_adapter.openhands.workspace_tool.subprocess.run") as run:
            run.return_value.stdout, run.return_value.stderr, run.return_value.returncode = "", "", 0
            self.tools.run_configured_command("target_test", 0)
        self.assertEqual(run.call_args.args[0][-3:], tuple(plan()["target_test"][0]["argv"]))

    def test_configured_cwd_escape_is_rejected_including_later_symlink(self):
        for cwd in ("../secret", "/tmp"):
            config = plan()
            config["target_test"][0]["cwd"] = cwd
            with self.assertRaises(ValueError):
                WorkspaceTools(self.tools.container, execution_plan=config, timeout_seconds=3, output_limit_bytes=100)
        (self.root / "build").rmdir()
        (self.root / "build").symlink_to(self.root.parent, target_is_directory=True)
        with patch("openhands_adapter.openhands.workspace_tool.subprocess.run") as run:
            self.assertEqual(self.tools.run_configured_command("target_test", 0).exit_code, 126)
        run.assert_not_called()

    def test_file_tools_read_search_and_edit_without_execution(self):
        (self.root / "source.py").write_text("old\n")
        with patch("openhands_adapter.openhands.workspace_tool.subprocess.run") as run:
            self.assertIn("source.py", self.tools.list_files())
            self.assertEqual(self.tools.read_file("source.py"), "1: old")
            self.assertIn("source.py:1: old", self.tools.search_text("old"))
            self.tools.write_file("source.py", "fixed\n", expected_content="old\n")
            self.tools.write_file("src/new.py", "new\n")
        run.assert_not_called()
        self.assertEqual((self.root / "source.py").read_text(), "fixed\n")
        with self.assertRaises(ValueError):
            self.tools.write_file("source.py", "stale\n", expected_content="old\n")

    def test_file_tools_block_internal_paths_tests_and_symlink_escapes(self):
        (self.root / ".agent-diet.failure.log").write_text("FAILED parser")
        self.assertIn("FAILED parser", self.tools.read_file(".agent-diet.failure.log"))
        (self.root / "link").symlink_to(self.root.parent, target_is_directory=True)
        for name in ("../secret", "/tmp/secret", "link/secret", ".git/config", ".agent-diet.repair-config.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.tools.read_file(name)
        for name in ("tests/test_parser.py", "fixtures/example.txt", "parser_test.go", ".agent-diet.failure.log", ".git/config"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.tools.write_file(name, "tampered")
        self.assertNotIn("link", self.tools.list_files())

    def test_hardlinked_file_and_fifo_cannot_be_written_or_read_as_regular_file(self):
        import os
        (self.root / "original").write_text("keep")
        os.link(self.root / "original", self.root / "alias")
        with self.assertRaises(ValueError):
            self.tools.write_file("alias", "tampered", "keep")
        os.mkfifo(self.root / "fifo")
        with self.assertRaises(ValueError):
            self.tools.read_file("fifo")

    def test_edit_requires_one_exact_match_and_protects_configured_scripts(self):
        (self.root / "source.py").write_text("old\n")
        self.tools.edit_file("source.py", "old", "new")
        self.assertEqual((self.root / "source.py").read_text(), "new\n")
        with self.assertRaises(ValueError):
            self.tools.edit_file("source.py", "missing", "replacement")
        (self.root / "source.py").write_text("same same")
        with self.assertRaises(ValueError):
            self.tools.edit_file("source.py", "same", "replacement")
        (self.root / "runtest").write_text("pytest tests/")
        config = plan()
        config["target_test"][0] = {"argv": ["bash", "-lc", "./runtest --only parser"], "cwd": "."}
        tools = WorkspaceTools(self.tools.container, execution_plan=config, timeout_seconds=3, output_limit_bytes=100)
        with self.assertRaisesRegex(ValueError, "protected_command_file"):
            tools.edit_file("runtest", "pytest", "echo")
        for argv, cwd in ((["bash", "../runtest"], "build"), (["bash", str(self.root / "runtest")], ".")):
            config["target_test"][0] = {"argv": argv, "cwd": cwd}
            tools = WorkspaceTools(self.tools.container, execution_plan=config, timeout_seconds=3, output_limit_bytes=100)
            with self.assertRaisesRegex(ValueError, "protected_command_file"):
                tools.edit_file("runtest", "pytest", "echo")

    def test_command_timeout_and_output_limit(self):
        with patch("openhands_adapter.openhands.workspace_tool.subprocess.run", side_effect=
                   subprocess.TimeoutExpired("docker", 13, output=b"x" * 5000)):
            result = self.tools.run_configured_command("regression_test", 0)
        self.assertEqual(result.exit_code, 124)
        self.assertTrue(result.timed_out)
        self.assertEqual(len(result.output), 4000)

    def test_diff_includes_modified_and_new_source(self):
        (self.root / "source.py").write_text("old\n")
        create_baseline(self.root)
        self.tools.write_file("source.py", "fixed\n", "old\n")
        self.tools.write_file("new.py", "new\n")
        result = self.tools.inspect_workspace_diff()
        self.assertEqual(result.exit_code, 0)
        self.assertIn("+fixed", result.output)
        self.assertIn("+++ b/new.py", result.output)
        self.assertIn("+new", result.output)


if __name__ == "__main__":
    unittest.main()
