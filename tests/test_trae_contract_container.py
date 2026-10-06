"""Opt-in image fixtures, no model/benchmark/network dependency."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from openhands.sdk import LLM
from openhands_adapter.openhands.container import start, remove
from openhands_adapter.openhands.trae_session import stage_tools, TraeSandbox, TOOL_ROOT, PYTHON
from openhands_adapter.openhands.trae_tools import parse_tool_response
from openhands_adapter.openhands.trae_agent import TraeContractAgent, TraeLocalConversation
from openhands_adapter.openhands.prompts import build_user_prompt
from openhands_adapter.workflow.patch import apply_patch
from trae_reference import assert_reference_hashes, response, oracle
from test_trae_contract_runtime import tool_namespace, reference_session

IMAGES = ['php-src/defect4c:latest', 'swebench/sweb.eval.x86_64.fmtlib_1776_fmt-3901:latest']


def call(session, name, args):
    return parse_tool_response(response((name,json.dumps(args,ensure_ascii=False)))[0], '', session)[0]['content']


@unittest.skipUnless(os.getenv('AGENTDIET_CONTAINER_INTEGRATION')=='1','requires local PHP/fmtlib images and Docker')
class ContainerIntegrationTests(unittest.TestCase):
    def setUp(self): assert_reference_hashes()

    def test_actual_wrappers_editor_history_long_output_shell_state_restart(self):
        for image in IMAGES:
            with self.subTest(image=image), tempfile.TemporaryDirectory() as td:
                root=Path(td); (root/'tests').mkdir(); p=root/'tests/repro.txt'
                operations=[dict(command='create',file_text='old\ttext\nline2\n'),dict(command='view'),
                    dict(command='str_replace',old_str='old',new_str='new'),dict(command='insert',insert_line=1,new_str='inserted\tline'),
                    dict(command='undo_edit'),dict(command='undo_edit')]
                container=start(root,image=image,exact_trae=True); session=None
                try:
                    stage_tools(container); sandbox=TraeSandbox(container)
                    source_session=reference_session(sandbox,time)
                    expected=[]
                    reference=oracle()['parse_tool_response']
                    for args in operations:
                        ans=response(('str_replace_editor',json.dumps(dict(path=str(p),**args),ensure_ascii=False)))[0]
                        content=reference(ans,'',source_session)[0]['content']
                        expected.append((content,p.read_bytes()))
                    p.unlink()
                    source_session.execute(f'rm -f {TOOL_ROOT}/file_history.pkl {TOOL_ROOT}/log.out')
                    session=sandbox.get_session()
                    for args,(output,file_bytes) in zip(operations,expected):
                        self.assertEqual(call(session,'str_replace_editor',dict(path=str(p),**args)),output)
                        self.assertEqual(p.read_bytes(),file_bytes)
                    ns=tool_namespace();editor=ns['EditTool']()
                    # Undo expected contents come from the source wrapper sequence.
                    editor._file_history[p].append(expected[0][1].decode())
                    # 60k output remains complete; the last newline is source stderr concatenation.
                    self.assertEqual(call(session,'bash',{'command':"printf '%60001s' x"}), ' '*60000+'x\n')
                    self.assertEqual(call(session,'bash',{'command':'printf "Unicode lỗi\\n"'}),'Unicode lỗi\n')
                    self.assertEqual(call(session,'bash',{'command':'cd /tmp; export TRAE_FIXTURE=yes; true'}),'\n')
                    self.assertEqual(call(session,'bash',{'command':'printf "%s" "${TRAE_FIXTURE-unset}"'}),'unset\n')
                    # Original editor clips raw content before numbering/tabs, not formatted output.
                    p.unlink()
                    p.write_text('\tUnicode lỗi\n'*2000)
                    expected_long=asyncio.run(editor(path=str(p),command='view')).output
                    self.assertEqual(call(session,'str_replace_editor',{'path':str(p),'command':'view'}),expected_long)
                    # A short real outer timeout and restart keep files and pickle history.
                    timed=session.execute('sleep 2',timeout=.05)
                    self.assertIn('Command timed out after 0.05 seconds. Partial output:',timed)
                    session=sandbox.get_session()
                    self.assertEqual(call(session,'str_replace_editor',{'path':str(p),'command':'undo_edit'}),
                                     asyncio.run(editor(path=str(p),command='undo_edit')).output)
                    self.assertFalse((root/'file_history.pkl').exists())
                    self.assertFalse((root/'log.out').exists())
                    # Verify the Python 3.10 fallback through the actual inner shell.
                    check=(f'cd {TOOL_ROOT} && {PYTHON} -c '+shlex.quote(
                        'import asyncio; from bash import BashTool, _BashSession; '
                        '_BashSession._timeout=.01; from execute_bash import execute_command; '
                        'print(asyncio.run(execute_command(command="sleep 1")))'))
                    self.assertIn('timed out: bash has not returned in 0.01 seconds',session.execute(check))
                finally:
                    if session: session.close()
                    remove(container)

    def test_scripted_sdk_reproduce_test_only_done_production_done_external_fail(self):
        for image in IMAGES:
            with self.subTest(image=image), tempfile.TemporaryDirectory() as td:
                root=Path(td); project=root/'repair';project.mkdir();(project/'tests').mkdir()
                (project/'source').write_text('old\n');(project/'tests/test_a').write_text('old test\n')
                def git(*args):return subprocess.run(('git','-C',str(project),*args),check=True,capture_output=True)
                git('init');git('config','user.email','test@local');git('config','user.name','test');git('add','.');git('commit','-m','base')
                container=start(project,image=image,exact_trae=True); conversation=None
                try:
                    stage_tools(container); sandbox=TraeSandbox(container)
                    scripts=[
                        response(('str_replace_editor',json.dumps({'command':'create','path':str(project/'reproduce.py'),'file_text':'print("reproduced")\n'})),
                                 ('bash',json.dumps({'command':f'python3 {shlex.quote(str(project/"reproduce.py"))}'})),
                                 ('str_replace_editor',json.dumps({'command':'str_replace','path':str(project/'tests/test_a'),'old_str':'old test','new_str':'new test'}))),
                        response(('think',' { "thought" : "plan" } ')),response(('task_done','{}')),
                        response(('str_replace_editor',json.dumps({'command':'str_replace','path':str(project/'source'),'old_str':'old','new_str':'new'}))),
                        response(('task_done',' { } ')),
                    ]
                    requests=[]; hooks=[];script=iter(scripts)
                    def transport(messages,tools):requests.append(deepcopy(messages));return next(script)
                    agent=TraeContractAgent(llm=LLM(model='openai/test',api_key='fake')).bind_runtime(
                        transport=transport,sandbox=sandbox,after_normal_turn=lambda mgr:hooks.append(mgr.count_turn()))
                    conversation=TraeLocalConversation(agent=agent,workspace=project,visualizer=None,stuck_detection=False,max_iteration_per_run=51)
                    conversation.send_message(build_user_prompt(project,problem_statement='fix issue'))
                    conversation.run(); snapshot=conversation.state.agent_state['trae_contract']
                    self.assertEqual(snapshot['gen'],'task_done');self.assertEqual(snapshot['turns'],5)
                    self.assertEqual(hooks,[1,2,3,4]);self.assertEqual(len(requests),5)
                    self.assertEqual(snapshot['steps'][0][2]['content'],'reproduced\n')
                    self.assertEqual(snapshot['steps'][2][-1]['content'],'ERROR! Your Patch is empty. Please provide a patch that fixes the problem.')
                    self.assertNotIn('tests/test_a',snapshot['patch']);self.assertNotIn('reproduce.py',snapshot['patch'])
                    validation=root/'validation';validation.mkdir();(validation/'source').write_text('old\n')
                    apply_patch(validation,snapshot['patch'].encode())
                    self.assertEqual((validation/'source').read_text(),'new\n')
                    # External verdict is separate; a failure cannot call transport again.
                    external=subprocess.run(('python3','-c','raise SystemExit(1)'),cwd=validation)
                    self.assertEqual(external.returncode,1);self.assertEqual(len(requests),5)
                finally:
                    if conversation:conversation.close()
                    remove(container)

    def test_D09_D11_completed_batches_compression_real_images(self):
        from openhands_adapter.config import AgentDietConfig
        from diet_contract_fixture import analyzer, assert_parity
        from trae_reference import diet_metrics
        for image in IMAGES:
            with self.subTest(image=image), tempfile.TemporaryDirectory() as td:
                root=Path(td); project=root/'repair'; project.mkdir()
                source=project/'source'; source.write_text('long Unicode Tiếng Việt line\n'*700+'old-marker\n')
                def git(*args): return subprocess.run(('git','-C',str(project),*args),check=True,capture_output=True)
                git('init'); git('config','user.email','test@local'); git('config','user.name','test'); git('add','.'); git('commit','-m','base')
                container=start(project,image=image,exact_trae=True); conversation=None
                try:
                    stage_tools(container); sandbox=TraeSandbox(container)
                    # Long observed output in a complete multi-tool batch exercises defaults.
                    script=iter([
                        response(('bash',json.dumps({'command':f'cat {shlex.quote(str(source))}'})),
                                 ('think','{"thought":"preserve marker"}'),content='visible thought'),
                        response(('think','{"thought":"next"}'),content='normal'),
                        response(('str_replace_editor',json.dumps({'path':str(source),'command':'str_replace','old_str':'old-marker','new_str':'new-marker'})),content='edit'),
                        response(('bash',json.dumps({'command':f'grep new-marker {shlex.quote(str(source))}'})),content='verify'),
                        response(('task_done','{}'),content='done')])
                    ns,calls,c,compressor_transport=analyzer(AgentDietConfig())
                    expected_metrics=diet_metrics(); requests=[]
                    def hook(mgr):
                        reference=oracle()['MessageManager']('', '', None, expected_metrics,True,50,[])
                        reference.user_message=deepcopy(mgr.user_message)
                        reference.steps=deepcopy(mgr.steps)
                        ns['maybe_perform_analysis_step'](reference)
                        c.after_normal_turn(mgr)
                        assert_parity(self,mgr,reference,c)
                    def transport(messages,tools): requests.append(deepcopy(messages)); return next(script)
                    agent=TraeContractAgent(llm=LLM(model='openai/test',api_key='fake')).bind_runtime(
                        transport=transport,sandbox=sandbox,after_normal_turn=hook)
                    conversation=TraeLocalConversation(agent=agent,workspace=project,visualizer=None,
                        stuck_detection=False,max_iteration_per_run=51)
                    conversation.send_message(build_user_prompt(project,problem_statement='fix marker'))
                    conversation.run(); snapshot=conversation.state.agent_state['trae_contract']
                    self.assertEqual(snapshot['gen'],'task_done')
                    self.assertEqual(snapshot['turns'],5)
                    self.assertEqual(c.metrics['erase_count'],1)
                    self.assertEqual(compressor_transport.requests,[r[1] for r in calls])
                    self.assertIn('<talk>visible thought</talk>',snapshot['steps'][0][0]['agent_erased'])
                    self.assertEqual(len(snapshot['steps'][0]),1)
                    self.assertIn('compressed for better efficiency) short',requests[3][2]['content'])
                    self.assertNotIn('agent_erased',json.dumps(requests[3]))
                    validation=root/'validation'; validation.mkdir()
                    (validation/'source').write_text('long Unicode Tiếng Việt line\n'*700+'old-marker\n')
                    apply_patch(validation,snapshot['patch'].encode())
                    external=subprocess.run(('python3','-c','from pathlib import Path; assert "new-marker" in Path("source").read_text()'),cwd=validation)
                    self.assertEqual(external.returncode,0)
                    self.assertEqual(len(requests),5)
                finally:
                    if conversation: conversation.close()
                    remove(container)
