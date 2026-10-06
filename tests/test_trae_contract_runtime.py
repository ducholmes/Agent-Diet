import ast
import asyncio
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from pathlib import Path
import os
import tempfile
import types
from typing import ClassVar, Literal, get_args
import unittest
from unittest.mock import patch
import pexpect
from openhands_adapter.openhands.trae_session import TraeShellSession, INITIAL_PROMPT_TIMEOUT, OUTER_TIMEOUT
from openhands_adapter.openhands.trae_tools import parse_tool_response
from trae_reference import ROOT, REFERENCE, assert_reference_hashes, load_nodes, oracle, response


def tool_namespace(runtime=False):
    ns = dict(dataclass=dataclass, fields=fields, replace=replace, asyncio=asyncio, defaultdict=defaultdict,
              Path=Path, Literal=Literal, ClassVar=ClassVar, get_args=get_args, os=os)
    names = {'ToolResult','CLIResult','ToolFailure','ToolError','TRUNCATED_MESSAGE','MAX_RESPONSE_LEN','maybe_truncate','run',
             'Command','SNIPPET_LINES','EditTool','_BashSession','BashTool'}
    if runtime:
        for filename in ['base.py','run.py','edit.py','bash.py']:
            source = (ROOT/'src/openhands_adapter/compat/tool_runtime'/filename).read_text()
            nodes = [n for n in ast.parse(source).body if getattr(n,'name',None) in names or isinstance(n,ast.Assign) and any(getattr(t,'id',None) in names for t in n.targets) or isinstance(n,ast.AnnAssign) and getattr(n.target,'id',None) in names]
            exec(compile(ast.Module(body=nodes,type_ignores=[]),filename,'exec'), ns)
        timeout_ns = dict(asyncio=asyncio)
        exec((ROOT/'src/openhands_adapter/compat/tool_runtime/timeout_compat.py').read_text(), timeout_ns)
        ns['polling_timeout'] = timeout_ns['timeout']
    else:
        for filename in ['base.py','run.py','edit.py','bash.py']:
            # Include annotated constants independently from the source.
            source = (REFERENCE/'tools/claude_tools'/filename).read_text()
            nodes = [n for n in ast.parse(source).body if getattr(n,'name',None) in names or isinstance(n,ast.Assign) and any(getattr(t,'id',None) in names for t in n.targets) or isinstance(n,ast.AnnAssign) and getattr(n.target,'id',None) in names]
            exec(compile(ast.Module(body=nodes,type_ignores=[]),str(REFERENCE/filename),'exec'),ns)
    return ns


class Clock:
    def __init__(self): self.sleeps=[]
    def time(self): return 100
    def sleep(self, delay): self.sleeps.append(delay)


class FakeShell:
    def __init__(self, output, timeout=False):
        self.output=output; self.timeout=timeout; self.commands=[]
        self.before=self.after=self.buffer=b''
    def sendline(self, cmd): self.commands.append(cmd)
    def expect(self, patterns, timeout):
        self.before, self.after, self.buffer = self.output
        if self.timeout: raise pexpect.TIMEOUT('fixture')
    def close(self, **kwargs): pass


def reference_session(sandbox, clock):
    source = (REFERENCE/'utils/sandbox.py').read_text()
    cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='Sandbox')
    get=deepcopy(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='get_session'))
    ns=dict(time=clock,pexpect=pexpect)
    exec(compile(ast.Module(body=[get],type_ignores=[]),'sandbox_oracle','exec'),ns)
    return ns['get_session'](sandbox)


class RuntimeTests(unittest.TestCase):
    def setUp(self): assert_reference_hashes()

    def test_shell_echo_status_unicode_long_output_and_timeout_match_source(self):
        for output, timeout in [
            ((b'echo\r\n'+('lỗi\n'+'x'*60001).encode()+b'\r\n',b'root@trae:/# ',b''),False),
            ((b'command\npartial\n',b'',b'tail\n'),True),
            ((b'echo\r\n\r\n',b'root@trae:/# ',b''),False),
        ]:
            for command in ['command', 'command &']:
                a=types.SimpleNamespace(shell=FakeShell(output,timeout),shell_ready_ts=101,start_shell=lambda:None)
                b=types.SimpleNamespace(shell=FakeShell(output,timeout),shell_ready_ts=101,start_shell=lambda:None)
                ca,cb=Clock(),Clock()
                expected=reference_session(a,ca).execute(command)
                with patch('openhands_adapter.openhands.trae_session.time',cb): actual=TraeShellSession(b).execute(command)
                self.assertEqual(actual,expected)
                self.assertEqual(a.shell.commands,b.shell.commands)
                self.assertEqual(ca.sleeps,cb.sleeps)
        self.assertEqual(INITIAL_PROMPT_TIMEOUT,10); self.assertEqual(OUTER_TIMEOUT,180)

    def test_editor_sequence_bytes_and_observations_match_independent_source(self):
        ref,actual=tool_namespace(),tool_namespace(True)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'test_repro.txt'
            long_text='\tUnicode lỗi\n'*2000
            async def exercise(ns):
                tool=ns['EditTool'](); out=[]
                for args in [dict(command='create',file_text='old\ttext\nline2\n'),dict(command='view'),
                             dict(command='str_replace',old_str='old',new_str='new'),
                             dict(command='insert',insert_line=1,new_str='inserted\tline'),
                             dict(command='undo_edit'),dict(command='undo_edit')]:
                    r=await tool(path=str(path),**args);out.append((r.output,r.error,path.read_bytes()))
                path.write_text(long_text)
                r=await tool(path=str(path),command='view');out.append((r.output,r.error,path.read_bytes()))
                return out
            expected=asyncio.run(exercise(ref));path.unlink()
            result=asyncio.run(exercise(actual))
            self.assertEqual(result,expected)
            self.assertIn(ref['TRUNCATED_MESSAGE'],result[-1][0])
            self.assertGreater(len(result[-1][0]),16000)
            self.assertEqual(actual['MAX_RESPONSE_LEN'],16000)

    def test_bash_long_output_state_and_inner_timeout_match_source(self):
        ref,actual=tool_namespace(),tool_namespace(True)
        async def run(ns, command, timeout=210):
            tool=ns['BashTool']()
            try:
                # A fresh BashTool per wrapper call.
                tool._session=ns['_BashSession']();tool._session._timeout=timeout
                await tool._session.start()
                result=await tool(command=command)
                return result.output,result.error
            except ns['ToolError'] as exc: return str(exc)
            finally:
                if tool._session and tool._session._started:
                    tool._session.stop()
                    await tool._session._process.communicate()
        with tempfile.TemporaryDirectory() as td:
            for command,timeout in [("printf '%60001s' x",210),('printf "Unicode lỗi\\n"; printf err >&2',210),('sleep 1',.01),('cd /tmp; export TRAE_FIXTURE=yes; true',210),('printf "%s" "${TRAE_FIXTURE-unset}"',210)]:
                self.assertEqual(asyncio.run(run(actual,command,timeout)),asyncio.run(run(ref,command,timeout)))
        self.assertEqual(ref['_BashSession']._timeout,210)
        self.assertEqual(ref['_BashSession']._output_delay,.2)
        self.assertEqual(ref['_BashSession']._sentinel,'<<exit>>')

    def test_python310_timeout_fallback_and_external_cancellation(self):
        ns=dict(asyncio=asyncio)
        exec((ROOT/'src/openhands_adapter/compat/tool_runtime/timeout_compat.py').read_text(),ns)
        async def exercise():
            with self.assertRaises(TimeoutError):
                async with ns['_PollingTimeout'](.01): await asyncio.sleep(1)
            async def cancel_me():
                async with ns['_PollingTimeout'](1): await asyncio.sleep(2)
            task=asyncio.create_task(cancel_me());await asyncio.sleep(0);task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        asyncio.run(exercise())

    def test_vendored_wrappers_equal_source_except_reviewed_timeout_boundary(self):
        runtime=ROOT/'src/openhands_adapter/compat/tool_runtime'
        for filename in ['base.py','run.py','edit.py','execute_bash.py','execute_str_replace_editor.py','bash.py']:
            expected=(REFERENCE/'tools/claude_tools'/filename).read_text()
            if filename=='bash.py':
                expected=expected.replace('import asyncio\n','import asyncio\nfrom timeout_compat import timeout as polling_timeout\n',1).replace('async with asyncio.timeout(self._timeout):','async with polling_timeout(self._timeout):')
            self.assertEqual((runtime/filename).read_text(),expected,filename)
