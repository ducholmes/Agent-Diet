import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from audit_fixture import exercise
from openhands_adapter.config import WorkflowConfig
from openhands_adapter.events import configure
from openhands_adapter.workflow.runner import run_case
from openhands_adapter.workflow.models import BaselineResult
from openhands_adapter.input_loader import load_case
from openhands_adapter.openhands.process import _capture_worker_stream
from openhands_adapter.compat.audit import register_secrets
import io


class RetentionTests(unittest.TestCase):
    def tearDown(self):configure(None)

    def test_A14_16_23_rerun_baseline_failure_has_fresh_identity_and_no_stale_archive(self):
        with tempfile.TemporaryDirectory() as td:
            _,output,*_=exercise(Path(td))
            first=json.loads((output/'audit/manifest.json').read_text())
            self.assertTrue((output/'protocol-manifest.json').is_file());self.assertTrue((output/'audit/sdk').is_dir())
            with patch('openhands_adapter.workflow.runner.run_baseline',return_value=BaselineResult(False,reason='fixture baseline failure')):
                result=run_case(load_case(Path(td)),WorkflowConfig(output_root=output.parent),reference_profile='trae_verified')
            second=json.loads((output/'audit/manifest.json').read_text())
            self.assertNotEqual(first['run_id'],second['run_id']);self.assertEqual(second['run_id'],result.run_id)
            for name in ('protocol-manifest.json','contract-manifest.json','contract-result.json','contract-patch.diff','patch.diff','response.txt','audit/sdk'):
                self.assertFalse((output/name).exists(),name)
            self.assertEqual(result.agent,'not_started');self.assertEqual(second['generation_status'],'not_started')
            with patch('audit_fixture._write_case'):
                failed,failed_output,requests,compressions,_=exercise(Path(td),fatal=True)
            third=json.loads((failed_output/'audit/manifest.json').read_text())
            self.assertNotIn(third['run_id'],(first['run_id'],second['run_id']))
            self.assertEqual(failed.agent,'generation_error');self.assertEqual(len(requests),3)
            self.assertEqual(len(compressions),1)
            snapshot=json.loads((failed_output/'contract-result.json').read_text())
            self.assertEqual(snapshot['run_id'],third['run_id']);self.assertEqual(snapshot['gen'],'generation_error')
            self.assertEqual(failed.patch_origin,'error_wip')

    def test_A14_18_worker_stream_retention_sanitizes_and_hashes_content(self):
        with tempfile.TemporaryDirectory() as td:
            secret='stream-sentinel-12345';register_secrets(secret)
            for raw in (True,False):
                path=Path(td)/str(raw)
                _capture_worker_stream(io.StringIO('arbitrary-history '+secret+'\n'),path,forward_progress=False,raw=raw)
                self.assertNotIn(secret,path.read_text())
                if not raw:self.assertNotIn('arbitrary-history',path.read_text())

    def test_P12_12_malformed_snapshot_never_recaptures_or_validates(self):
        with tempfile.TemporaryDirectory() as td:
            from test_input_loader import _write_case
            from openhands_adapter.workflow.patch import create_baseline
            root=Path(td);_write_case(root,'case');case=load_case(root)
            def baseline(case,repair,validation,**kwargs):create_baseline(repair);return BaselineResult(True)
            def worker(workspace,output):
                (workspace/'source.c').write_text('valid WIP\n');(output/'contract-result.json').write_text('{broken')
                return 'done'
            with patch('openhands_adapter.workflow.runner.run_baseline',side_effect=baseline), \
                 patch('openhands_adapter.workflow.trae_patch.capture_filtered_patch',side_effect=AssertionError('recaptured invalid snapshot')), \
                 patch('openhands_adapter.workflow.runner.run_post_patch',side_effect=AssertionError('evaluated invalid snapshot')):
                result=run_case(case,WorkflowConfig(output_root=root/'out'),agent_runner=worker,reference_profile='trae_verified')
            self.assertEqual(result.agent,'contract_artifact_error');self.assertFalse(result.patch_applied)

    def test_A14_18_external_logs_sanitize_copy_without_changing_verdict_operands(self):
        from openhands_adapter.workflow.command import _write_log
        from openhands_adapter.workflow.models import CommandResult
        from openhands_adapter.workflow.validation import _command_verdict
        from openhands_adapter.input_loader import CommandSpec
        secret='external-log-sentinel-12345';register_secrets(secret)
        value=CommandResult(('echo',secret),'.',0,0,secret+'\n','')
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'command.log';_write_log(path,value)
            self.assertNotIn(secret,path.read_text())
            self.assertEqual(value.stdout,secret+'\n')
            self.assertTrue(_command_verdict(CommandSpec(('echo',secret),evidence_pattern=secret),value,test=True)[0])
