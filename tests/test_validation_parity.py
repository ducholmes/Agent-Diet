"""Differential evidence tests against the local ContextSniper evaluator."""
from __future__ import annotations
import importlib.util
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from openhands_adapter.workflow.validation import run_post_patch
from openhands_adapter.workflow.validation_reference import ProjectValidator
from openhands_adapter.workflow.validation_outcome import classify_patch_outcome
from openhands_adapter.workflow.models import CommandResult
from openhands_adapter.input_loader import CommandSpec
from test_validation_suite import case

REFERENCE = Path(__file__).resolve().parents[2] / 'ContextSniper/ContextSniper-Codex'
sys.path.insert(0, str(REFERENCE))
from evaluator.validator import ProjectValidator as Oracle
from evaluator.outcome import classify_patch_outcome as oracle_classify

class ParityTests(unittest.TestCase):
    def run_results(self, outputs, selected=None):
        selected = selected or case()
        results = [CommandResult(('test',), '.', code, .1, text, '') for code, text in outputs]
        with patch('openhands_adapter.workflow.validation.ensure_available'), patch(
                'openhands_adapter.workflow.validation.run_command', side_effect=results) as run:
            result = run_post_patch(selected, Path('/tmp'), timeout_seconds=5)
        return result, run

    def test_nonefix_and_event_reports_failed(self):
        with patch('openhands_adapter.workflow.validation.stage') as events:
            result, run = self.run_results([(1, 'FAILED broken'), (0, 'PASSED stable')])
        self.assertEqual(result.status, 'nonefix')
        self.assertEqual(result.regression_failures, ())
        self.assertEqual(result.target_failures, ('broken',))
        self.assertEqual(run.call_count, 2)
        target = [c for c in events.call_args_list if c.kwargs.get('phase') == 'target' and len(c.args) > 1 and c.args[1] == 'phase_finished'][0]
        self.assertEqual(target.kwargs['result'], 'failed')

    def test_duplicate_old_failure_is_not_regression(self):
        result, _ = self.run_results([(1, 'FAILED broken'), (1, 'FAILED broken')])
        self.assertEqual(result.status, 'nonefix')
        self.assertEqual(result.regression_test_ids, ())

    def test_negfix_and_noisefix(self):
        result, _ = self.run_results([(1, 'FAILED broken'), (1, 'FAILED stable')])
        self.assertEqual(result.status, 'negfix')
        self.assertEqual(result.regression_test_ids, ('stable',))
        selected = replace(case(), failing_tests=('broken', 'other'))
        result, run = self.run_results([(0, 'PASSED broken'), (1, 'FAILED other'), (1, 'FAILED stable')], selected)
        self.assertEqual(result.status, 'noisefix')
        self.assertEqual(run.call_count, 3)

    def test_wrong_or_missing_target_id(self):
        for text in ('PASSED wrong', 'PASS', 'SKIPPED broken'):
            result, run = self.run_results([(0, text)])
            self.assertEqual(result.status, 'invalid')
            self.assertEqual(run.call_count, 1)

    def test_status_conflict_and_unverified_failure(self):
        for code, text, reason in [(0, 'FAILED broken', 'test_status_output_conflict'),
                                   (1, 'PASSED broken', 'test_failure_unverified')]:
            result, _ = self.run_results([(code, text)])
            self.assertEqual(result.status, 'invalid')
            self.assertIn(reason, result.reason)

    def test_repeat_setup_and_build(self):
        selected = replace(case(), setup_commands=(CommandSpec(('setup',)),),
                           build_commands=(CommandSpec(('build',)),))
        result, run = self.run_results([(0, ''), (0, ''), (0, 'PASSED broken'),
                                       (0, ''), (0, ''), (0, 'PASSED stable')], selected)
        self.assertEqual(result.status, 'plausible')
        labels = [c.kwargs['label'] for c in run.call_args_list]
        self.assertEqual(len(set(labels)), 6)
        self.assertEqual(labels[3], 'patched-regression-setup-1')

    def test_full_suite_only(self):
        result, _ = self.run_results([(0, 'PASSED broken')], replace(case(), target_test_commands=()))
        self.assertEqual(result.status, 'plausible')
        self.assertFalse(result.evidence['target_executed'])

    def test_regression_setup_failure_emits_invalid(self):
        selected = replace(case(), setup_commands=(CommandSpec(("setup",)),))
        with patch("openhands_adapter.workflow.validation.stage") as events:
            result, _ = self.run_results([(0, ""), (0, "PASSED broken"), (1, "setup error")], selected)
        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.evidence["regression_status"], "invalid")
        finished = [c for c in events.call_args_list if len(c.args) > 1 and
                    c.args[1] == "phase_finished" and c.kwargs.get("phase") == "regression"]
        self.assertEqual(finished[0].kwargs["result"], "invalid")

    def test_empty_baseline_does_not_fall_back(self):
        from openhands_adapter.workflow.validation import external_baseline
        self.assertEqual(external_baseline(case(), ())['failed_test_ids'], [])
        self.assertEqual(external_baseline(case(), None)['failed_test_ids'], ['broken'])

    def test_config_change_rejected_before_patch_application(self):
        from test_output_cleanup import OutputCleanupTests
        from openhands_adapter.workflow.runner import run_case
        from openhands_adapter.workflow.models import BaselineResult
        from openhands_adapter.config import WorkflowConfig
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = OutputCleanupTests().case(root)
            selected.config_path.write_text('{}')
            def agent(workspace, output):
                selected.config_path.write_text('{"system": "changed"}')
                return 'done'
            with patch('openhands_adapter.workflow.runner.run_baseline', return_value=BaselineResult(True)), \
                 patch('openhands_adapter.workflow.runner.capture_patch', return_value=b'nonempty patch'), \
                 patch('openhands_adapter.workflow.runner.apply_patch') as apply:
                result = run_case(selected, WorkflowConfig(output_root=root / 'output'), agent_runner=agent)
            self.assertIn('validation_plan_changed_before_patch', result.reason)
            apply.assert_not_called()

    def test_parser_matches_reference(self):
        for text in ('PASSED a\nFAILED b', 'PASSED a\nFAILED a', 'SKIPPED a', 'FAIL a'):
            self.assertEqual(ProjectValidator._protocol_test_results(text), Oracle._protocol_test_results(text))
        for left, right in [('a::b', 'b'), ('test', 'other_test'), ('a#b', 'a::b')]:
            self.assertEqual(ProjectValidator._test_ids_match(left, right), Oracle._test_ids_match(left, right))

    def test_structured_report_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / '.debugging-framework/test.xml'
            report.parent.mkdir()
            report.write_text('<testsuite tests="1"><testcase name="a"/></testsuite>')
            before = ProjectValidator._test_report_state(root)
            self.assertEqual(ProjectValidator._test_case_results(root, before, (), []), Oracle._test_case_results(root, before, (), []))
            report.write_text('<testsuite tests="1" failures="1"><testcase name="a"><failure/></testcase></testsuite>')
            self.assertEqual(ProjectValidator._test_case_results(root, before, (), []), Oracle._test_case_results(root, before, (), []))
            self.assertEqual(ProjectValidator._test_case_results(root, before, (), [])[1], 'structured-report')

    def test_orchestration_matches_reference(self):
        from openhands_adapter.workflow.validation import _ReferenceExecutor, ReferenceExecutor, _reference_spec
        class OracleExecutor(_ReferenceExecutor, Oracle):
            pass
        selected = case()
        spec = _reference_spec(selected.regression_test_commands[0], "regression")
        plan = SimpleNamespace(system="custom", setup=(), build=(), regression_test=(spec,))
        for outputs in ([(0, "PASSED broken"), (0, "PASSED stable")],
                        [(1, "FAILED broken"), (0, "PASSED stable")],
                        [(1, "FAILED broken"), (1, "FAILED broken")],
                        [(0, "PASSED wrong")], [(0, "FAILED broken")],
                        [(1, "PASSED broken")], [(0, "PASSED broken"), (1, "FAIL stable")]):
            snapshots = []
            for executor_type in (ReferenceExecutor, OracleExecutor):
                executor = executor_type(selected, 5, None)
                results = [CommandResult(("test",), ".", code, .1, text, "") for code, text in outputs]
                with patch("openhands_adapter.workflow.validation.run_command", side_effect=results):
                    snapshots.append(executor._run_target_and_regression(Path("/tmp"), plan, None, selected.failing_tests))
            self.assertEqual(snapshots[0], snapshots[1])

    def test_classifier_matches_reference(self):
        for initial in ([], ['a'], ['a', 'b']):
            for post in ([], ['a'], ['b'], ['a', 'c']):
                for source in ('framework-marker', 'command'):
                    baseline = dict(status='failing' if initial else 'plausible', failed_test_ids=initial, test_id_source='caller-supplied')
                    patched = dict(status='failing' if post else 'plausible', failed_test_ids=post, test_id_source=source)
                    self.assertEqual(classify_patch_outcome(baseline, patched), oracle_classify(baseline, patched))

if __name__ == '__main__':
    unittest.main()
