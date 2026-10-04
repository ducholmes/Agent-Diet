from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.config import DEFAULT_OPENHANDS_MODEL, OpenHandsConfig
from openhands_adapter.openhands.llm import OPENROUTER_BASE_URL, build_llm


class FakeLLM:
    calls: list[tuple[str, dict]] = []

    def __init__(self, **kwargs):
        self.calls.append(("constructor", kwargs))

    @classmethod
    def subscription_login(cls, **kwargs):
        cls.calls.append(("subscription_login", kwargs))
        return kwargs


class LLMTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeLLM.calls.clear()
        self.modules = {
            "openhands": types.ModuleType("openhands"),
            "openhands.sdk": types.SimpleNamespace(LLM=FakeLLM),
        }

    def test_model_environment_variable_is_ignored(self) -> None:
        with patch.dict(os.environ, {"OPENHANDS_MODEL": "env-model"}, clear=True):
            config = OpenHandsConfig.from_env()
        self.assertEqual(config.model, DEFAULT_OPENHANDS_MODEL)

    def test_subscription_uses_openhands_login(self) -> None:
        with patch("openhands_adapter.openhands.llm.require_pinned_sdk"), patch.dict(sys.modules, self.modules), patch.dict(os.environ, {}, clear=True):
            build_llm(OpenHandsConfig(model="gpt-5.2-codex", auth="subscription"))
        self.assertEqual(FakeLLM.calls, [("subscription_login", {"vendor": "openai", "model": "gpt-5.2-codex", "reasoning_effort": "low", "auth_method": "browser", "force_login": False})])

    def test_openrouter_key_infers_compatible_endpoint(self) -> None:
        config = OpenHandsConfig(model="openai/gpt-5.2", auth="api-key", api_key_env="OPENROUTER_API_KEY")
        with patch("openhands_adapter.openhands.llm.require_pinned_sdk"), patch.dict(sys.modules, self.modules), patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=True):
            build_llm(config)
        self.assertEqual(FakeLLM.calls, [("constructor", {"model": "openai/gpt-5.2", "api_key": "test-key", "reasoning_effort": "low", "base_url": OPENROUTER_BASE_URL})])

    def test_reasoning_default_survives_worker_config_roundtrip(self) -> None:
        from dataclasses import asdict
        self.assertEqual(OpenHandsConfig.from_mapping({}).reasoning_effort, "low")
        with patch("openhands_adapter.openhands.llm.require_pinned_sdk"), patch.dict(sys.modules, self.modules), patch.dict(os.environ, {}, clear=True):
            config = OpenHandsConfig.from_mapping(asdict(OpenHandsConfig.from_env()))
            build_llm(config)
        self.assertEqual(FakeLLM.calls[-1][1]["reasoning_effort"], "low")

    def test_explicit_reasoning_effort_reaches_both_sdk_auth_paths(self) -> None:
        for auth in ("subscription", "api-key"):
            for effort in ("high", None):
                with self.subTest(auth=auth, effort=effort):
                    config = OpenHandsConfig.from_mapping({"auth": auth, "reasoning_effort": effort})
                    with patch("openhands_adapter.openhands.llm.require_pinned_sdk"), patch.dict(sys.modules, self.modules), patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
                        build_llm(config)
                    self.assertEqual(FakeLLM.calls[-1][1]["reasoning_effort"], effort)

    def test_subscription_rejects_openrouter_base_url(self) -> None:
        config = OpenHandsConfig(auth="subscription", base_url=OPENROUTER_BASE_URL)
        with patch("openhands_adapter.openhands.llm.require_pinned_sdk"), patch.dict(sys.modules, self.modules):
            with self.assertRaisesRegex(ValueError, "cannot use base_url"):
                build_llm(config)


if __name__ == "__main__":
    unittest.main()
