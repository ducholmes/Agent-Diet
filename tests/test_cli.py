from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.cli import _override, _run_selected_case, build_parser, main
from openhands_adapter.config import DEFAULT_OUTPUT_ROOT, RunConfig
from openhands_adapter.input_loader import CommandSpec, load_case
from openhands_adapter.openhands.prompts import build_user_prompt
from openhands_adapter.openhands.process import WorkerResult
from openhands_adapter.workflow.patch import create_baseline
from openhands_adapter.workflow.validation import _expand_targets
from openhands_adapter.workflow.workspace import create_workspaces
from test_input_loader import _write_case


class CliTests(unittest.TestCase):
    def test_worker_commands_preserve_shell_wrappers_and_match_evaluator_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_case(root, "case")
            case = replace(
                load_case(root, "case"), setup_commands=(),
                failing_tests=("test one", "test/two"),
                target_test_commands=(CommandSpec((
                    "bash", "-lc", "printf '%s\\n' '{test_id}'; exit 0",
                ), cwd="build"),),
                regression_test_commands=(CommandSpec((
                    "runner", "--test", "other", "--exclude-test", "excluded",
                )),),
            )

            def fake_run_case(case, workflow, *, agent_runner, reference_profile="generic", run_config=None):
                agent_runner(root / "workspace", root / "output")
                return SimpleNamespace(resolved=False, validation="not_run", reason="no_patch")

            args = build_parser().parse_args(["--model", "test-model"])
            with patch("openhands_adapter.cli.run_case", side_effect=fake_run_case), \
                 patch("openhands_adapter.cli.run_worker", return_value=WorkerResult("", 0, 0.0, False)) as worker:
                _run_selected_case(case, RunConfig(), args, "Repair the failure.")
            plan = worker.call_args.kwargs["execution_plan"]
            self.assertEqual(plan["setup"], [])
            self.assertEqual(
                [entry["argv"] for entry in plan["target_test"]],
                [list(spec.argv) for spec, _ in _expand_targets(case)],
            )
            for entry in plan["target_test"]:
                self.assertEqual(entry["cwd"], "build")
                self.assertEqual(set(entry), {"argv", "cwd"})
            self.assertEqual(plan["regression_test"][0]["argv"], [
                "runner", "--test", "other", "--exclude-test", "excluded",
            ])
            self.assertEqual(set(plan), {"setup", "build", "target_test", "regression_test"})

    def test_selected_case_delivers_buggy_source_and_failure_log_to_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case_id = "CVE-2017-7272__bab0b99f376d"
            _write_case(root, case_id)
            _write_case(root, "other-case")
            failure = "FAILED selected-test\nExpected safe behavior, got a crash.\n"
            (root / f"{case_id}.failure.log").write_text(failure)

            def fake_run_case(case, workflow, *, agent_runner, reference_profile="generic", run_config=None):
                self.assertEqual(case.case_id, case_id)
                spaces = create_workspaces(case.source_project, parent=root)
                try:
                    create_baseline(spaces.repair, case.failure_log)
                    agent_runner(spaces.repair, root / "output")
                finally:
                    spaces.cleanup()
                return SimpleNamespace(
                    resolved=False, validation="not_run", reason="no_patch",
                    to_dict=lambda: {},
                )

            def fake_worker(workspace, prompt, output, **kwargs):
                self.assertEqual(
                    (workspace / "source.c").read_text(),
                    (root / case_id / "source.c").read_text(),
                )
                self.assertNotEqual(workspace.resolve(), (root / case_id).resolve())
                self.assertEqual((workspace / ".agent-diet.failure.log").read_text(), failure)
                self.assertIn(str(workspace.resolve()), prompt)
                self.assertIn("[Buggy source code]", prompt)
                self.assertIn("[Problem statement]", prompt)
                self.assertIn(failure, prompt)
                self.assertIn("Read .agent-diet.failure.log", prompt)
                self.assertIn("Focus on the parser.", prompt)
                self.assertIn("run_configured_command", prompt)
                self.assertFalse((workspace / ".agent-diet.repair-config.json").exists())
                self.assertIn("target_test", kwargs["execution_plan"])
                self.assertNotIn("[Configured build and test scope]", prompt)
                self.assertEqual(kwargs["image"], "example/repair:latest")
                return WorkerResult("", 0, 0.0, False)

            with patch("openhands_adapter.cli.run_case", side_effect=fake_run_case), \
                 patch("openhands_adapter.cli.run_worker", side_effect=fake_worker) as worker:
                self.assertEqual(main([
                    "--input", str(root), "--case", case_id, "--model", "test-model",
                    "--reference-profile", "generic", "--prompt", "Focus on the parser.",
                ]), 1)
                worker.assert_called_once()

    def test_multiple_cases_run_in_order_with_their_own_worker_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("case-a", "case-b"):
                _write_case(root, name)
            calls = []

            def fake_run_case(case, workflow, *, agent_runner, reference_profile="generic", run_config=None):
                calls.append(case.case_id)
                agent_runner(case.source_project, root / "output" / case.case_id)
                return SimpleNamespace(
                    resolved=case.case_id == "case-a", validation="not_run", reason="test",
                    to_dict=lambda: {"case_id": case.case_id},
                )

            def fake_worker(workspace, prompt, output, **kwargs):
                self.assertIn(str(workspace.resolve()), prompt)
                self.assertEqual(output.name, workspace.name)
                return WorkerResult("", 0, 0.0, False)

            with patch("openhands_adapter.cli.run_case", side_effect=fake_run_case), \
                 patch("openhands_adapter.cli.run_worker", side_effect=fake_worker):
                self.assertEqual(main([
                    "--input", str(root), "--model", "test-model",
                    "--case", "case-b", "case-a", "--case", "case-b",
                ]), 1)
            self.assertEqual(calls, ["case-b", "case-a"])

    def test_all_cases_continues_after_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("case-b", "case-a"):
                _write_case(root, name)
            result = SimpleNamespace(resolved=True, to_dict=lambda: {"resolved": True})
            with patch("openhands_adapter.cli._run_selected_case", side_effect=[
                RuntimeError("case failed"), result,
            ]) as run:
                self.assertEqual(main([
                    "--input", str(root), "--model", "test-model", "--all-cases",
                ]), 1)
            self.assertEqual([call.args[0].case_id for call in run.call_args_list], ["case-a", "case-b"])

    def test_batch_resolves_every_selector_before_starting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_case(root, "case-a")
            with patch("openhands_adapter.cli.run_case") as run:
                with self.assertRaises(SystemExit):
                    main(["--input", str(root), "--model", "test-model",
                          "--case", "case-a", "missing"])
                run.assert_not_called()

    def test_all_cases_cannot_be_combined_with_selected_cases(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--model", "test-model", "--all-cases", "--case", "case-a"])

    def test_custom_task_prompt_keeps_mandatory_case_context(self) -> None:
        prompt = build_user_prompt(
            Path("/repair/project"), problem_statement="Unexpected return value.",
            instructions="Focus on the parser.", reference_profile="generic",
        )
        for expected in (
            "/repair/project", "Unexpected return value.", "Focus on the parser.",
            ".agent-diet.failure.log", "production code", "test fixtures",
            "evaluator configuration", "Agent-Diet artifacts",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, prompt)

    def test_task_prompt_without_problem_statement_uses_failure_log(self) -> None:
        prompt = build_user_prompt(Path("/repair/project"), reference_profile="generic")
        self.assertNotIn("[Problem statement]", prompt)
        self.assertIn("Read .agent-diet.failure.log", prompt)

    def test_model_is_required_on_every_cli_run(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--input", "/tmp/prepared", "--case", "case-1"])

    def test_native_style_and_diet_options_override_config(self) -> None:
        args = build_parser().parse_args([
            "--input", "/tmp/prepared", "--case", "case-1", "--output", "exp1",
            "--model", "openai/gpt-5.2", "--auth", "api-key", "--api-key-env", "OPENROUTER_API_KEY",
            "--reference-profile", "generic", "--openhands-timeout", "20", "--command-timeout", "10", "--timeout", "30",
            "--diet-mode", "delete", "--diet-threshold", "42", "--ctx-before", "3", "--hide-context",
        ])
        config = _override(RunConfig(), args, args.input_option.resolve())
        self.assertEqual(config.openhands.model, "openai/gpt-5.2")
        self.assertEqual(config.openhands.api_key_env, "OPENROUTER_API_KEY")
        self.assertEqual(config.workflow.agent_timeout_seconds, 20)
        self.assertEqual(config.workflow.command_timeout_seconds, 10)
        self.assertEqual(config.workflow.validation_timeout_seconds, 30)
        self.assertEqual(config.agentdiet.mode, "delete")
        self.assertEqual(config.agentdiet.threshold_tokens, 42)
        self.assertEqual(config.agentdiet.ctx_before, 3)
        self.assertFalse(config.agentdiet.show_ctx)
        self.assertEqual(config.workflow.output_root, DEFAULT_OUTPUT_ROOT / "exp1")

    def test_output_subdirectories_are_independent_of_current_directory(self):
        for value, relative in (("exp1", "exp1"), ("output/exp1", "exp1"), ("runs/ours", "runs/ours"), ("output", ".")):
            with self.subTest(value=value):
                args = build_parser().parse_args(["--model", "test-model", "--output", value])
                self.assertEqual(_override(RunConfig(), args, None).workflow.output_root, DEFAULT_OUTPUT_ROOT / relative)

    def test_output_cannot_escape_output_directory(self):
        for value in ("../elsewhere", "/tmp/out"):
            with self.subTest(value=value):
                args = build_parser().parse_args(["--model", "test-model", "--output", value])
                with self.assertRaisesRegex(ValueError, "subdirectory"):
                    _override(RunConfig(), args, None)

    def test_config_output_uses_the_same_output_root(self):
        config = RunConfig.from_mapping({"workflow": {"output_root": "runs/ours"}})
        args = build_parser().parse_args(["--model", "test-model"])
        self.assertEqual(_override(config, args, None).workflow.output_root, DEFAULT_OUTPUT_ROOT / "runs/ours")
        with self.assertRaisesRegex(ValueError, "subdirectory"):
            RunConfig.from_mapping({"workflow": {"output_root": "/tmp/out"}})

    def test_monetary_budget_configuration_is_not_silently_ignored(self):
        from openhands_adapter.config import OpenHandsConfig
        from unittest.mock import patch
        with self.assertRaises(SystemExit):
            build_parser().parse_args(['--model', 'test-model', '--max-budget', '1'])
        with self.assertRaisesRegex(ValueError, 'tracking tokens only'):
            OpenHandsConfig.from_mapping({'max_budget': 1})
        with patch.dict('os.environ', {'OPENHANDS_MAX_BUDGET': '1'}):
            with self.assertRaisesRegex(ValueError, 'tracking tokens only'):
                OpenHandsConfig.from_env()

    def test_default_output_is_inside_agent_diet(self) -> None:
        args = build_parser().parse_args([
            "--model", "test-model", "--input", "/tmp/prepared", "--case", "case-1"
        ])
        config = _override(RunConfig(), args, args.input_option.resolve())
        self.assertEqual(config.workflow.output_root, DEFAULT_OUTPUT_ROOT)

    def test_prompt_options_are_mutually_checked_by_main_helper(self) -> None:
        args = build_parser().parse_args([
            "--model", "test-model", "--prompt", "one", "--prompt-file", "/tmp/two"
        ])
        from openhands_adapter.cli import _prompt
        with self.assertRaisesRegex(ValueError, "either --prompt or --prompt-file"):
            _prompt(args)


if __name__ == "__main__":
    unittest.main()
