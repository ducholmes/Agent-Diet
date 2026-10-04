from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.input_loader import load_case
from openhands_adapter.workflow.patch import PatchError, assert_safe_patch, capture_patch
from openhands_adapter.workflow.validation import run_baseline
from openhands_adapter.workflow.workspace import create_workspaces
from test_input_loader import _write_case


class WorkspaceTests(unittest.TestCase):
    def test_execution_config_is_not_written_into_any_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_case(root, "case")
            case = load_case(root, "case")
            spaces = create_workspaces(case.source_project, parent=root)
            try:
                with patch("openhands_adapter.workflow.validation.ensure_available"):
                    result = run_baseline(case, spaces.repair, spaces.validation)
                self.assertTrue(result.clean, result.reason)
                filename = ".agent-diet.repair-config.json"
                path = spaces.repair / filename
                self.assertFalse(path.exists())
                self.assertFalse((spaces.validation / filename).exists())
                self.assertFalse((case.source_project / filename).exists())
                self.assertEqual(capture_patch(spaces.repair), b"")
                path.write_text("{}\n")
                with self.assertRaises(PatchError):
                    assert_safe_patch(capture_patch(spaces.repair))
            finally:
                spaces.cleanup()

    def test_legacy_workspace_config_is_not_copied_from_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_case(root, "case")
            case = load_case(root, "case")
            filename = ".agent-diet.repair-config.json"
            legacy = case.source_project / filename
            legacy.write_text("{}\n")
            spaces = create_workspaces(case.source_project, parent=root)
            try:
                self.assertFalse((spaces.repair / filename).exists())
                self.assertFalse((spaces.validation / filename).exists())
                self.assertEqual(legacy.read_text(), "{}\n")
                self.assertEqual((spaces.repair / "source.c").read_bytes(),
                                 (case.source_project / "source.c").read_bytes())
                with patch("openhands_adapter.workflow.validation.ensure_available"):
                    result = run_baseline(case, spaces.repair, spaces.validation)
                self.assertTrue(result.clean, result.reason)
                self.assertEqual(capture_patch(spaces.repair), b"")
            finally:
                spaces.cleanup()


if __name__ == "__main__":
    unittest.main()
