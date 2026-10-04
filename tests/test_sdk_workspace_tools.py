"""Exercise real pinned SDK schemas/executors without network or an LLM call."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openhands_adapter.openhands.runtime import OPENHANDS_SDK_VERSION

try:
    HAS_SDK = version("openhands-sdk") == OPENHANDS_SDK_VERSION
except PackageNotFoundError:
    HAS_SDK = False


@unittest.skipUnless(HAS_SDK, "Pinned OpenHands SDK is not installed")
class SdkWorkspaceToolsTests(unittest.TestCase):
    def test_resolved_tools_reject_arbitrary_execution_and_operate_on_files(self):
        with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True", "OPENHANDS_SUPPRESS_BANNER": "1"}):
            from openhands.sdk.tool import resolve_tool
            from pydantic import ValidationError
            from openhands_adapter.openhands.container import RepairContainer
            from openhands_adapter.openhands.workspace_tool import WorkspaceTools, workspace_tool_specs, TOOL_NAMES

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = WorkspaceTools(RepairContainer("repair", root, "image"),
                                   execution_plan={phase: [] for phase in ("setup", "build", "target_test", "regression_test")},
                                   timeout_seconds=1, output_limit_bytes=4000)
            resolved = [resolve_tool(spec, None)[0] for spec in workspace_tool_specs(tools)]
            self.assertEqual(tuple(tool.name for tool in resolved), TOOL_NAMES)
            by_name = {tool.name: tool for tool in resolved}
            run = by_name["run_configured_command"]
            for extra in ({"command": "pytest tests/"}, {"argv": ["bash"]}, {"cwd": ".."}):
                with self.subTest(extra=extra), self.assertRaises(ValidationError):
                    run.action_type(phase="target_test", index=0, **extra)
            for fields in ({"phase": "shell", "index": 0}, {"phase": "target_test", "index": -1},
                           {"phase": "target_test", "index": True}):
                with self.subTest(fields=fields), self.assertRaises(ValidationError):
                    run.action_type(**fields)
            with patch("openhands_adapter.openhands.workspace_tool.subprocess.run") as execute:
                self.assertEqual(run(run.action_type(phase="target_test", index=0)).exit_code, 126)
                write = by_name["write_file"]
                self.assertEqual(write(write.action_type(path="a.py", content="old\n")).exit_code, 0)
                edit = by_name["edit_file"]
                self.assertEqual(edit(edit.action_type(path="a.py", old_text="old", new_text="new")).exit_code, 0)
                read = by_name["read_file"]
                self.assertIn("new", read(read.action_type(path="a.py")).output)
                self.assertEqual(read(read.action_type(path="../secret")).exit_code, 126)
                self.assertEqual(write(write.action_type(path="tests/test_a.py", content="tampered")).exit_code, 126)
            execute.assert_not_called()
