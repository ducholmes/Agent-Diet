"""D12 oracle checks across real Git states and failure boundaries."""
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from openhands_adapter.workflow.trae_patch import capture_source_diff, remove_patches_to_tests, validate_snapshot
from openhands_adapter.openhands.trae_session import TraeSandbox
from openhands_adapter.openhands.container import RepairContainer
from trae_reference import load_nodes, oracle


class PatchContractTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(('git', '-C', str(root), *args), stderr=subprocess.DEVNULL)

    def test_P12_01_05_06_git_states_against_frozen_helper(self):
        helper = load_nodes('tools/get_diff.py', {'git_diff_to_patch'}, {'os': os, 'subprocess': subprocess})['git_diff_to_patch']
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'source').write_text('one\ntwo\nthree\n'); (root/'tests').mkdir()
            (root/'tests/t').write_text('old\n'); self.git(root, 'init')
            self.git(root, 'config', 'user.email', 'test@local'); self.git(root, 'config', 'user.name', 'test')
            self.git(root, 'add', '.'); self.git(root, 'commit', '-m', 'base')
            def compare():
                cwd=Path.cwd(); output=StringIO(); before=self.git(root,'ls-files','--stage')
                try:
                    with redirect_stdout(output): helper(str(root))
                finally: os.chdir(cwd)
                actual=capture_source_diff(root)
                self.assertEqual(actual, output.getvalue())
                self.assertEqual(remove_patches_to_tests(actual), oracle()['remove_patches_to_tests'](output.getvalue()))
                self.assertEqual(self.git(root,'ls-files','--stage'), before)
                return actual
            self.assertEqual(compare(), '\n')
            (root/'source').write_text('changed\ntwo\nthree\n'); (root/'tests/t').write_text('new\n')
            for name in ('new_source', 'reproducer.py', 'tests/new_test'):
                (root/name).write_text('untracked\n')
            self.assertIn('+changed', compare()); self.assertNotIn('new_source', compare())
            self.git(root, 'add', 'source'); self.assertNotIn('source b/source', compare())
            (root/'source').write_text('changed\ntwo\nchanged three\n')
            partial=compare(); self.assertIn('+changed three', partial); self.assertNotIn('+changed\n', partial)
            self.git(root, 'add', '.'); self.git(root, 'commit', '-m', 'agent commit'); self.assertEqual(compare(), '\n')
            (root/'intentional').write_text('added\n'); self.git(root, 'add', '-N', 'intentional')
            self.assertIn('b/intentional', compare())

    def test_P12_02_04_07_literal_filter_quirks(self):
        source=oracle()['remove_patches_to_tests']
        paths=['test/x','tests/x','testing/x','test_x','a.tests.c','a.test.c','a_test_b','a_tests_b',
               'a_test.c','a_tests.c','a.spec.ts','tox.ini.bak','Cargo.lock','package.json',
               'package-lock.json','pom.xml','ext/demo/sample.phpt','tests/x.phpt','Tests/a','source.c']
        fixtures=['', ' \n\t', 'preamble\r\n', 'diff --git x y\nkeep\n',
                  'diff --git "a/tests/space x" "b/tests/space x"\nkeep per source split\n',
                  'diff --git a/tests/old b/src/new\nrename from tests/old\nrename to src/new\n',
                  'diff --git a/src/old b/tests/new\nrename to tests/new\n']
        fixtures += [f'diff --git a/{p} b/{p}\nBinary files differ\nold mode 100644\nnew mode 100755\n' for p in paths]
        fixtures += [''.join(fixtures), 'diff --git a/deleted b/deleted\ndeleted file mode 100644\n--- a/deleted\n+++ /dev/null\n']
        for value in fixtures:
            with self.subTest(value=value[:80]): self.assertEqual(remove_patches_to_tests(value), source(value))

    def test_P12_11_git_and_decode_error_are_source_diagnostics(self):
        import sys
        helper = Path(__file__).resolve().parents[1] / 'artifact/artifact/code/trae_agent/tools/get_diff.py'
        for body in ("return b'\\xff'", "raise subprocess.CalledProcessError(1, ['git','--no-pager','diff','--ignore-submodules=all'])"):
            script = ("import sys, runpy, subprocess\n"
                "def injected(*args, **kwargs):\n    " + body + "\n"
                "subprocess.check_output=injected\n"
                "sys.argv=[sys.argv[1],'-p','/tmp']\nrunpy.run_path(sys.argv[0],run_name='__main__')")
            expected=subprocess.run((sys.executable,'-c',script,str(helper)),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=True).stdout
            with patch('openhands_adapter.workflow.trae_patch.subprocess.run', return_value=SimpleNamespace(stdout=expected)):
                self.assertEqual(capture_source_diff(Path('/tmp')),expected.decode(errors='replace'))
            self.assertTrue(expected.startswith(b'git diff error:  '))
        with tempfile.TemporaryDirectory() as td:
            # Actual failed Git emits stderr before the source's caught diagnostic.
            expected=subprocess.run((sys.executable,str(helper),'-p',td),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=True).stdout
            self.assertEqual(capture_source_diff(Path(td)),expected.decode(errors='replace'))
        with tempfile.TemporaryDirectory() as td:
            file=Path(td)/'file';file.touch()
            expected=subprocess.run((sys.executable,str(helper),'-p',str(file)),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=True).stdout
            self.assertEqual(capture_source_diff(file),expected.decode(errors='replace'))

    def test_P12_11_outer_exec_retries_only_runtime_failure(self):
        ns=load_nodes('utils/sandbox.py', {'Sandbox'}, {'Container':object})
        source=object.__new__(ns['Sandbox']); sleeps=[]
        source.container=SimpleNamespace(exec_run=lambda cmd: (_ for _ in ()).throw(OSError('runtime')))
        with patch.object(ns['time'], 'sleep', side_effect=sleeps.append), redirect_stdout(StringIO()):
            expected=source.get_diff_result('/testbed')
        sandbox=TraeSandbox(RepairContainer('fixture',Path('/testbed'),'image'))
        actual_sleeps=[]
        with patch.object(sandbox,'_exec_diff_helper', side_effect=OSError('runtime')) as run, \
             patch('openhands_adapter.openhands.trae_session.time.sleep', side_effect=actual_sleeps.append):
            self.assertEqual(sandbox.get_diff(),expected)
        self.assertEqual(run.call_count,3);self.assertEqual(actual_sleeps,sleeps)
        with patch.object(sandbox,'_exec_diff_helper',return_value=b'git diff error:  inner\n') as run:
            self.assertEqual(sandbox.get_diff(),'git diff error:  inner\n'); self.assertEqual(run.call_count,1)

    def test_P12_12_snapshot_schema_consistency_and_profiles(self):
        good={'schema_version':1,'run_id':'fixture','profile':'trae_verified','gen':'task_done','turns':1,'original_patch':'diff --git a/a b/a\n',
              'filtered_patch':'diff --git a/a b/a\n','patch':'diff --git a/a b/a\n','patch_origin':'terminal_snapshot'}
        self.assertEqual(validate_snapshot(good,'trae_verified'),good)
        for updates in ({'schema_version':True},{'patch':2},{'turns':True},{'turns':51},{'profile':'trae_multiswe'},
                        {'filtered_patch':'tamper'},{'patch':'tamper'},{'gen':'turn_capped'},
                        {'patch_origin':'host'},{'gen':'invalid'}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_snapshot({**good,**updates},'trae_verified')
        with self.assertRaises(ValueError):validate_snapshot({'gen':'task_done','patch':'legacy'},'trae_verified')

    def test_P12_baseline_rejects_ignored_production_input_without_auto_inclusion(self):
        from test_input_loader import _write_case
        from openhands_adapter.input_loader import load_case
        from openhands_adapter.workflow.validation import run_baseline
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);_write_case(root,'case');case=load_case(root)
            repair=root/'repair';validation=root/'validation';repair.mkdir();validation.mkdir()
            for workspace in (repair,validation):
                (workspace/'source.c').write_text('old\n');(workspace/'.gitignore').write_text('source.c\n')
            with patch('openhands_adapter.workflow.validation.ensure_available'):
                result=run_baseline(case,repair,validation)
            self.assertFalse(result.clean);self.assertIn('provisioning',result.reason)
            self.assertNotIn(b'source.c',self.git(repair,'ls-files'))

    def test_P12_exec_api_demultiplexes_stderr_and_preserves_inner_exit_domain(self):
        import httpx
        import struct
        sandbox=TraeSandbox(RepairContainer('fixture',Path('/testbed'),'image'))
        requests=[]
        def frame(stream,data):return bytes([stream,0,0,0])+struct.pack('>I',len(data))+data
        def handler(request):
            import json
            requests.append((request.url.path,json.loads(request.content)))
            if request.url.path.endswith('/exec'):return httpx.Response(201,json={'Id':'exec-fixture'})
            return httpx.Response(200,content=frame(2,b'fatal: Git stderr\n')+frame(1,b'git diff error:  inner\n'))
        client=httpx.Client(transport=httpx.MockTransport(handler),base_url='http://docker')
        with patch('httpx.Client',return_value=client):
            self.assertEqual(sandbox.get_diff(),'fatal: Git stderr\ngit diff error:  inner\n')
        self.assertEqual(len(requests),2)
        self.assertEqual(requests[0][1]['Cmd'][-2:],['-p','/testbed'])
