"""A14-22: full HTTP/SDK/tool/diet/submission bundle on both real images."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from audit_fixture import exercise
from openhands_adapter.compat.audit import verify_bundle
from openhands_adapter.events import configure

@unittest.skipUnless(os.getenv('AGENTDIET_CONTAINER_INTEGRATION') == '1', 'opt-in Docker integration')
class AuditContainerTests(unittest.TestCase):
    def tearDown(self):configure(None)
    def test_A14_22_full_audit_on_php_and_fmtlib(self):
        images=('php-src/defect4c:latest','swebench/sweb.eval.x86_64.fmtlib_1776_fmt-3901:latest')
        for image in images:
            with self.subTest(image=image),tempfile.TemporaryDirectory() as td:
                result,output,requests,compressions,_=exercise(Path(td),real_image=image)
                self.assertEqual(result.agent,'task_done',result.error)
                self.assertTrue(result.patch_applied);self.assertFalse(result.resolved)
                self.assertEqual(len(requests),4);self.assertEqual(len(compressions),1)
                self.assertEqual(result.diet_metrics['erase_count'],1)
                self.assertEqual(result.validation,'nonefix')
                self.assertTrue(result.validation_summary['environment_ready'])
                self.assertTrue((output/'logs/validation').is_dir())
                self.assertTrue((output/'audit/sdk').is_dir())
                self.assertEqual(verify_bundle(output,full=True)['status'],'pass')
                self.assertFalse((output/'.tmp').exists())
