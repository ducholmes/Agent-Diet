import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from openhands_adapter.events import identity
from openhands_adapter.config import WorkflowConfig, RunConfig
from openhands_adapter.workflow.models import BaselineResult, ValidationResult
from openhands_adapter.workflow.runner import run_case
from openhands_adapter.workflow.patch import create_baseline
from openhands_adapter.input_loader import load_case
from test_input_loader import _write_case


class WorkflowTests(unittest.TestCase):
    def test_runner_submits_accepted_filtered_patch_even_if_workspace_later_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);_write_case(root,'case');case=load_case(root)
            output=root/'output'
            accepted='diff --git a/source.c b/source.c\n--- a/source.c\n+++ b/source.c\n@@ -1 +1 @@\n-old\n+accepted\n'
            runs=[]
            def baseline(case,repair,validation,**kwargs):
                (repair/'source.c').write_text('old\n');(validation/'source.c').write_text('old\n')
                create_baseline(repair)
                return BaselineResult(True)
            def agent(workspace,artifacts):
                runs.append(workspace);(workspace/'source.c').write_text('different after stop\n')
                (artifacts/'contract-result.json').write_text(json.dumps({'schema_version':1,'run_id':identity()['run_id'],'case_id':identity()['case_id'],'profile':'trae_verified','gen':'task_done','patch':accepted,'turns':1,
                    'original_patch':accepted,'filtered_patch':accepted,'patch_origin':'terminal_snapshot'}))
                (artifacts/'contract-patch.diff').write_text(accepted)
                return 'done'
            def validate(case,workspace,**kwargs):
                self.assertEqual((workspace/'source.c').read_text(),'accepted\n')
                return ValidationResult(False,'target',reason='external_fail',status='failed')
            with patch('openhands_adapter.workflow.runner.run_baseline',side_effect=baseline), \
                 patch('openhands_adapter.workflow.runner.run_post_patch',side_effect=validate), \
                 patch('openhands_adapter.workflow.runner.capture_patch',side_effect=AssertionError('legacy capture used')):
                result=run_case(case,WorkflowConfig(output_root=output),agent_runner=agent,reference_profile='trae_verified')
            self.assertEqual(result.agent,'task_done');self.assertFalse(result.resolved)
            self.assertEqual(len(runs),1)
            self.assertEqual((output/'case/patch.diff').read_text(),accepted)

    def test_exact_cli_overrides_and_watchdog_disabled_are_explicit(self):
        from openhands_adapter.cli import build_parser, _override
        args=build_parser().parse_args(['--model','fake','--max-iterations','500'])
        with self.assertRaisesRegex(ValueError,'generic'):_override(RunConfig(),args,None)
        args=build_parser().parse_args(['--model','fake','--agent-timeout','0'])
        self.assertIsNone(_override(RunConfig(),args,None).workflow.agent_timeout_seconds)
        args=build_parser().parse_args(['--model','fake','--agent-timeout','-1'])
        with self.assertRaises(ValueError):_override(RunConfig(),args,None)
