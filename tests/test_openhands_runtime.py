from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.openhands import runtime


class OpenHandsRuntimeTests(unittest.TestCase):
    def test_pinned_sdk_version_is_accepted(self) -> None:
        with patch("openhands_adapter.openhands.runtime.version", return_value="1.49.5"):
            runtime.require_pinned_sdk()

    def test_unreviewed_sdk_version_is_rejected(self) -> None:
        with patch("openhands_adapter.openhands.runtime.version", return_value="9.9.9"):
            with self.assertRaisesRegex(RuntimeError, "Unsupported OpenHands SDK version"):
                runtime.require_pinned_sdk()

    def test_tool_policy_is_an_exact_allowlist(self) -> None:
        self.assertEqual(runtime.OPENHANDS_SDK_VERSION, "1.49.5")
        import re
        from openhands_adapter.openhands.workspace_tool import WORKSPACE_TOOL_REGEX, TOOL_NAMES

        for name in TOOL_NAMES:
            self.assertIsNotNone(re.fullmatch(WORKSPACE_TOOL_REGEX, name))
        for name in ("workspace_shell", "terminal", "run_configured_command_extra"):
            self.assertIsNone(re.fullmatch(WORKSPACE_TOOL_REGEX, name))


if __name__ == "__main__":
    unittest.main()
