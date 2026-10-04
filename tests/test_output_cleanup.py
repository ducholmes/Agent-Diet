from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.config import AgentDietConfig, OpenHandsConfig, WorkflowConfig
from openhands_adapter.input_loader import CaseSpec, EnvironmentSpec
from openhands_adapter.openhands.container import RepairContainer
from openhands_adapter.openhands.process import run_worker
from openhands_adapter.workflow.models import BaselineResult, ValidationResult
from openhands_adapter.workflow.runner import run_case
from openhands_adapter.workflow.workspace import create_workspaces


class OutputCleanupTests(unittest.TestCase):
    def test_read_only_build_directory_is_removed_without_touching_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self.case(root)
            spaces = create_workspaces(case.source_project, parent=root)
            build = spaces.validation / "build"
            build.mkdir()
            (build / "object.o").write_text("compiled")
            build.chmod(0o500)
            spaces.cleanup()
            self.assertFalse(spaces.root.exists())
            self.assertEqual((case.source_project / "source.txt").read_text(), "source")

    def test_cleanup_failure_is_reported_instead_of_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spaces = create_workspaces(self.case(root).source_project, parent=root)
            with patch("openhands_adapter.workflow.workspace.shutil.rmtree", side_effect=PermissionError("foreign owner")):
                with self.assertRaisesRegex(PermissionError, "foreign owner"):
                    spaces.cleanup()
            spaces.cleanup()

    def case(self, root):
        source = root / "source"
        source.mkdir()
        (source / "source.txt").write_text("source")
        return CaseSpec(
            "case", "case", root, source, root / "config.json", root / "failure.log", 6,
            EnvironmentSpec("image", "docker", "image"), (), (), (), (), (), (), True, True,
        )

    def test_normal_run_keeps_results_without_workspace_or_empty_log_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self.case(root)
            output = root / "output" / "case"
            (output / "logs").mkdir(parents=True)
            (output / "logs" / "worker-config.json").write_text("stale")
            (output / "validation" / "logs").mkdir(parents=True)
            workspaces = []

            def agent(workspace, output):
                workspaces.append(workspace.parent)
                self.assertTrue(workspace.is_relative_to(output / ".tmp"))
                return "done"

            with patch("openhands_adapter.workflow.runner.run_baseline", return_value=BaselineResult(True)), patch("openhands_adapter.workflow.runner.capture_patch", return_value=b""):
                run_case(case, WorkflowConfig(output_root=root / "output"), agent_runner=agent)
            self.assertEqual({p.name for p in output.iterdir()}, {"result.json", "events.jsonl", "response.txt", "patch.diff"})
            self.assertEqual([p.name for p in output.parent.iterdir()], ["case"])
            self.assertFalse(workspaces[0].exists())
            self.assertEqual((case.source_project / "source.txt").read_text(), "source")

    def test_rerun_baseline_failure_removes_previous_patch_and_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self.case(root)
            output = root / 'output' / 'case'
            output.mkdir(parents=True)
            for name in ('result.json', 'patch.diff', 'response.txt', 'baseline.json', 'validation.json', 'diet-events.jsonl'):
                (output / name).write_text('previous run')
            with patch('openhands_adapter.workflow.runner.run_baseline', return_value=BaselineResult(False, reason='baseline failed')):
                result = run_case(case, WorkflowConfig(output_root=root / 'output'))
            self.assertEqual(result.baseline, 'failed')
            self.assertEqual({path.name for path in output.iterdir()}, {'result.json', 'events.jsonl'})
            self.assertEqual(json.loads((output / 'result.json').read_text())['baseline'], 'failed')

    def test_workspace_setup_failure_removes_empty_temporary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self.case(root)
            with patch('openhands_adapter.workflow.runner.create_workspaces', side_effect=OSError('copy failed')):
                with self.assertRaisesRegex(OSError, 'copy failed'):
                    run_case(case, WorkflowConfig(output_root=root / 'output'))
            output = root / 'output' / 'case'
            self.assertFalse((output / '.tmp').exists())
            self.assertTrue((output / 'events.jsonl').is_file())

    def test_agent_failure_cleans_workspace_and_preserves_diagnostic_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self.case(root)
            workspaces = []

            def agent(workspace, output):
                workspaces.append(workspace.parent)
                (output / "logs").mkdir()
                (output / "logs" / "worker.stderr.log").write_text("failure details")
                raise RuntimeError("agent failed")

            with patch("openhands_adapter.workflow.runner.run_baseline", return_value=BaselineResult(True)), patch("openhands_adapter.workflow.runner.capture_patch", return_value=b""):
                result = run_case(case, WorkflowConfig(output_root=root / "output"), agent_runner=agent)
            output = root / "output" / "case"
            self.assertEqual(result.agent, "failed")
            self.assertFalse(workspaces[0].exists())
            self.assertEqual((output / "logs" / "worker.stderr.log").read_text(), "failure details")
            self.assertTrue((output / "result.json").is_file())

    def test_failed_worker_recovers_tokens_and_compression_counters_from_events(self):
        from openhands_adapter.events import emit
        from openhands_adapter.token_tracking import response_tokens
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self.case(root)

            def agent(workspace, output):
                emit('llm_call_finished', call_id='repair', role='repair', model='shared', status='responded',
                     **response_tokens({'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}))
                emit('diet_analysis_started', mode='ours', step_index=1)
                emit('diet_step_change', status='accepted', before_token_estimate=200, after_token_estimate=40)
                emit('diet_sdk_condensation', before_token_estimate=500, expected_after_token_estimate=360, reminder_token_estimate=10)
                emit('diet_rejection', reason='compressor_error')
                emit('llm_call_started', call_id='pending', role='compression', model='shared', status='running', usage_status='unknown')
                raise RuntimeError('worker interrupted')

            with patch('openhands_adapter.workflow.runner.run_baseline', return_value=BaselineResult(True)), patch('openhands_adapter.workflow.runner.capture_patch', return_value=b''):
                result = run_case(case, WorkflowConfig(output_root=root / 'output'), agent_runner=agent)
            self.assertEqual(result.token_usage['repair']['total_tokens'], 120)
            self.assertIsNone(result.token_usage['total']['total_tokens'])
            self.assertEqual(result.token_usage['total']['known_tokens']['total_tokens'], 120)
            self.assertEqual(result.diet_metrics['analysis_count'], 1)
            self.assertEqual(result.diet_metrics['erase_count'], 1)
            self.assertEqual(result.diet_metrics['compression_llm_calls'], 1)
            self.assertEqual(result.diet_metrics['rejected'], {'compressor_error': 1})
            self.assertEqual(result.diet_metrics['reminder_token_estimate'], 10)
            self.assertEqual(result.diet_metrics['view_reduction_token_estimate'], 140)
            self.assertIsNone(result.diet_metrics['compression_total_tokens'])
            saved = json.loads((root / 'output' / 'case' / 'result.json').read_text())
            self.assertEqual(saved['token_usage'], result.token_usage)
            self.assertNotIn('cost', json.dumps(saved))

    def test_keep_workspaces_is_an_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self.case(root)
            workspaces = []

            def agent(workspace, output):
                workspaces.append(workspace.parent)
                return "done"

            with patch("openhands_adapter.workflow.runner.run_baseline", return_value=BaselineResult(True)), patch("openhands_adapter.workflow.runner.capture_patch", return_value=b""):
                run_case(case, WorkflowConfig(output_root=root / "output", keep_workspaces=True), agent_runner=agent)
            self.assertTrue(workspaces[0].is_relative_to(root / "output"))
            self.assertTrue((workspaces[0] / "repair" / "source.txt").is_file())

    def test_validation_logs_are_grouped_under_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = self.case(root)

            def validate(case, workspace, **kwargs):
                log_dir = kwargs["log_dir"]
                self.assertEqual(log_dir, root / "output" / "case" / "logs" / "validation")
                log_dir.mkdir(parents=True)
                (log_dir / "target.log").write_text("PASS")
                return ValidationResult(True, "complete", status="plausible")

            with (
                patch("openhands_adapter.workflow.runner.run_baseline", return_value=BaselineResult(True)),
                patch("openhands_adapter.workflow.runner.capture_patch", return_value=b"patch"),
                patch("openhands_adapter.workflow.runner.assert_safe_patch"),
                patch("openhands_adapter.workflow.runner.apply_patch"),
                patch("openhands_adapter.workflow.runner.run_post_patch", side_effect=validate),
            ):
                run_case(case, WorkflowConfig(output_root=root / "output"), agent_runner=lambda w, o: "done")
            self.assertFalse((root / "output" / "case" / "validation").exists())
            self.assertEqual((root / "output" / "case" / "logs" / "validation" / "target.log").read_text(), "PASS")

    def test_worker_rejects_output_inside_repair_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("openhands_adapter.openhands.process.start") as start:
                with self.assertRaisesRegex(ValueError, "outside the repair workspace"):
                    run_worker(root, "repair", root / "output", execution_plan={}, image="image", runtime="docker",
                               openhands=OpenHandsConfig(), diet=AgentDietConfig(), workflow=WorkflowConfig())
            start.assert_not_called()

    def test_worker_config_is_temporary_and_empty_logs_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_paths = []

            def spawn(argv, **kwargs):
                config_path = Path(argv[-1])
                config_paths.append(config_path)
                self.assertTrue(config_path.is_relative_to(root / ".tmp"))
                self.assertFalse(config_path.is_relative_to(root / "workspace"))
                config = json.loads(config_path.read_text())
                self.assertIn("execution_plan", config)
                Path(config["response_path"]).write_text("done")
                return types.SimpleNamespace(
                    stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO(),
                    poll=lambda: 0, returncode=0, pid=123,
                )

            (root / "workspace").mkdir()
            container = RepairContainer("repair", root / "workspace", "image")
            with patch("openhands_adapter.openhands.process.start", return_value=container), patch("openhands_adapter.openhands.process.remove") as remove, patch("openhands_adapter.openhands.process.subprocess.Popen", side_effect=spawn):
                result = run_worker(root / "workspace", "repair", root, execution_plan={phase: [] for phase in ("setup", "build", "target_test", "regression_test")}, image="image", runtime="docker", openhands=OpenHandsConfig(), diet=AgentDietConfig(), workflow=WorkflowConfig())
            self.assertEqual(result.response, "done")
            self.assertFalse(config_paths[0].parent.exists())
            self.assertEqual({p.name for p in root.iterdir()}, {"response.txt", "workspace"})
            remove.assert_called_once_with(container)

    def test_worker_start_failure_still_cleans_config_and_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_paths = []

            def spawn(argv, **kwargs):
                config_paths.append(Path(argv[-1]))
                self.assertTrue(config_paths[0].is_file())
                raise OSError("spawn failed")

            (root / "workspace").mkdir()
            container = RepairContainer("repair", root / "workspace", "image")
            with patch("openhands_adapter.openhands.process.start", return_value=container), patch("openhands_adapter.openhands.process.remove") as remove, patch("openhands_adapter.openhands.process.subprocess.Popen", side_effect=spawn):
                with self.assertRaisesRegex(OSError, "spawn failed"):
                    run_worker(root / "workspace", "repair", root, execution_plan={phase: [] for phase in ("setup", "build", "target_test", "regression_test")}, image="image", runtime="docker", openhands=OpenHandsConfig(), diet=AgentDietConfig(), workflow=WorkflowConfig())
            self.assertFalse(config_paths[0].parent.exists())
            self.assertFalse((root / "logs").exists())
            remove.assert_called_once_with(container)

    def test_timeout_cleans_temporary_config_and_retains_error_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_paths = []

            class Worker:
                pid = 123
                returncode = None
                stdin = io.StringIO()
                stdout = io.StringIO()
                stderr = io.StringIO("timeout details\n")

                def poll(self):
                    return self.returncode

                def wait(self, timeout):
                    self.returncode = -15
                    return self.returncode

            def spawn(argv, **kwargs):
                config_paths.append(Path(argv[-1]))
                return Worker()

            (root / "workspace").mkdir()
            container = RepairContainer("repair", root / "workspace", "image")
            with (
                patch("openhands_adapter.openhands.process.start", return_value=container),
                patch("openhands_adapter.openhands.process.remove") as remove,
                patch("openhands_adapter.openhands.process.subprocess.Popen", side_effect=spawn),
                patch("openhands_adapter.openhands.process.os.killpg") as kill,
                patch("openhands_adapter.openhands.process.time.monotonic", side_effect=[0, 2, 3, 4]),
            ):
                result = run_worker(root / "workspace", "repair", root, execution_plan={phase: [] for phase in ("setup", "build", "target_test", "regression_test")}, image="image", runtime="docker", openhands=OpenHandsConfig(), diet=AgentDietConfig(), workflow=WorkflowConfig(agent_timeout_seconds=1))
            self.assertTrue(result.timed_out)
            self.assertEqual(result.returncode, -15)
            kill.assert_called_once()
            remove.assert_called_once_with(container)
            self.assertFalse(config_paths[0].parent.exists())
            self.assertEqual((root / "logs" / "worker.stderr.log").read_text(), "timeout details\n")
            self.assertFalse((root / "logs" / "worker.stdout.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
