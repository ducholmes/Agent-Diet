from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.config import AgentDietConfig, OpenHandsConfig, WorkflowConfig
from openhands_adapter.openhands.agent import build_agent
from openhands_adapter.openhands.container import RepairContainer
from openhands_adapter.openhands.prompts import REPAIR_SYSTEM_PROMPT
from openhands_adapter.openhands.workspace_tool import WORKSPACE_TOOL_REGEX


class FakeAgent:
    calls: list[dict] = []

    def __init__(self, **kwargs):
        self.calls.append(kwargs)


class AgentAssemblyTests(unittest.TestCase):
    def test_generic_agent_receives_only_restricted_tools_and_reviewed_defaults(self) -> None:
        FakeAgent.calls.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            container = RepairContainer("repair", root, "prepared:image")
            sdk = types.SimpleNamespace(Agent=FakeAgent, AgentContext=types.SimpleNamespace)
            with (
                patch("openhands_adapter.openhands.agent.require_pinned_sdk"),
                patch("openhands_adapter.openhands.agent.build_sdk_condenser", return_value="diet-sdk-condenser"),
                patch("openhands_adapter.openhands.agent.build_llm", return_value=object()),
                patch("openhands_adapter.openhands.agent.workspace_tool_specs", return_value=["restricted-spec"]),
                patch.dict(sys.modules, {"openhands": types.ModuleType("openhands"), "openhands.sdk": sdk}),
            ):
                build_agent(
                    container,
                    OpenHandsConfig(reference_profile="generic"),
                    AgentDietConfig(),
                    WorkflowConfig(),
                    execution_plan={phase: [] for phase in ("setup", "build", "target_test", "regression_test")},
                )
        self.assertEqual(len(FakeAgent.calls), 1)
        self.assertEqual(FakeAgent.calls[0]["tools"], ["restricted-spec"])
        self.assertEqual(FakeAgent.calls[0]["condenser"], "diet-sdk-condenser")
        self.assertEqual(FakeAgent.calls[0]["filter_tools_regex"], WORKSPACE_TOOL_REGEX)
        self.assertEqual(FakeAgent.calls[0]["include_default_tools"], ["FinishTool", "ThinkTool"])
        self.assertEqual(
            FakeAgent.calls[0]["agent_context"].system_message_suffix,
            REPAIR_SYSTEM_PROMPT,
        )
        self.assertNotIn("system_prompt", FakeAgent.calls[0])


if __name__ == "__main__":
    unittest.main()
