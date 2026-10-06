from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from openhands.sdk import LLM
from openhands_adapter.compat.trae_contract import PROFILES
from openhands_adapter.openhands.trae_agent import TraeContractAgent, TraeLocalConversation
from openhands_adapter.openhands.prompts import build_user_prompt
from openhands_adapter.workflow.trae_patch import capture_filtered_patch, remove_patches_to_tests
from trae_reference import assert_reference_hashes, oracle, run_expert, response


class Sandbox:
    def __init__(self, outputs=(), patches=('diff --git a/a b/a\n',)):
        self.outputs = iter(outputs); self.patches = iter(patches)
        self.sessions = 0; self.closed = 0; self.commands = []
    def get_session(self): self.sessions += 1; return self
    def close(self): self.closed += 1
    def execute(self, cmd): self.commands.append(cmd); return next(self.outputs)
    def get_diff(self): return next(self.patches)
    def get_diff_result(self, path): return self.get_diff()


class ScriptedTransport:
    def __init__(self, responses):
        self.script = iter(deepcopy(responses)); self.requests = []
    def __call__(self, messages, tools):
        self.requests.append((deepcopy(messages), deepcopy(tools)))
        return next(self.script)


def run_sdk(responses, sandbox, *, profile='trae_verified', issue='Lỗi\n  preserved\ttext', hook=None, persistence=None):
    transport = ScriptedTransport(responses)
    agent = TraeContractAgent(llm=LLM(model='openai/test', api_key='fake'), reference_profile=profile).bind_runtime(
        transport=transport, sandbox=sandbox, after_normal_turn=hook)
    with tempfile.TemporaryDirectory() as td:
        conversation = TraeLocalConversation(agent=agent, workspace=td, persistence_dir=persistence,
            visualizer=None, stuck_detection=False, max_iteration_per_run=PROFILES[profile][0]+1)
        try:
            conversation.send_message(build_user_prompt(Path('/testbed'), problem_statement=issue))
            conversation.run()
            snapshot = deepcopy(conversation.state.agent_state['trae_contract'])
            status = conversation.state.execution_status.value
        finally:
            conversation.close()
    assert status == 'finished', status
    return transport.requests, snapshot['steps'], snapshot['gen'], snapshot['patch']


class TurnTests(unittest.TestCase):
    def setUp(self): assert_reference_hashes()

    def compare(self, responses, outputs=(), patches=('diff --git a/a b/a\n',), profile='trae_verified'):
        hooks_a, hooks_b = [], []
        a, b = Sandbox(outputs, patches), Sandbox(outputs, patches)
        with redirect_stdout(StringIO()):
            expected = run_expert(responses, a, profile=profile, hook=lambda mgr: hooks_a.append(mgr.count_turn()))
            actual = run_sdk(responses, b, profile=profile, hook=lambda mgr: hooks_b.append(mgr.count_turn()))
        self.assertEqual(actual, expected)
        self.assertEqual(a.commands, b.commands)
        self.assertEqual(hooks_a, hooks_b)
        self.assertEqual(a.sessions, b.sessions)
        self.assertEqual(b.closed, 1)
        return actual

    def test_plain_text_invalid_unknown_think_batch_terminal(self):
        for profile in PROFILES:
            self.compare([
                response(content='thinking'), response(('bash','bad json')),
                response(('unknown','{}')), response(('think',' { "thought" : "one" } ')),
                response(('task_done','{}'), ('bash','{"command":"echo hi"}'), ('think','{"thought":"two"}')),
                response(('task_done','{}')),
            ], outputs=['', 'Tool Call Status: 0\nhi\n'], profile=profile)

    def test_empty_and_test_only_done_persist_feedback(self):
        for empty in ['', '\n', 'diff --git a/tests/a b/tests/a\n+test\n']:
            actual = self.compare([response(('task_done','{}')), response(('task_done','{}'))], patches=[empty, 'diff --git a/a b/a\n'])
            self.assertEqual(actual[1][0][-1]['content'], 'ERROR! Your Patch is empty. Please provide a patch that fixes the problem.')
            self.assertEqual(len(actual[1][-1]), 1)

    def test_task_failed_and_missing_usage(self):
        self.compare([response(('task_failed','{}'))])
        self.compare([response(('task_done','{}'), usage=None), response(('bash','bad'), usage=None), response(('task_done','{}'))])

    def test_full_profiles_no_extra_model_request_and_final_hook(self):
        for profile, (cap, _) in PROFILES.items():
            script = [response(('think','{"thought":"repeat"}')) for _ in range(cap)]
            actual = self.compare(script, patches=['diff --git a/a b/a\nWIP\n'], profile=profile)
            self.assertEqual(len(actual[0]), cap)
            self.assertEqual(actual[2], 'turn_capped')
            self.assertEqual(len(actual[1]), cap)

    def test_timeout_restarts_outer_session(self):
        self.compare([response(('bash','{"command":"long"}')), response(('task_done','{}'))],
                     outputs=['Command timed out after 180 seconds. Partial output:\n + kept'])

    def test_source_diff_filter_handles_untracked_staged_committed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            def git(*args): return subprocess.run(('git','-C',td,*args),check=True,capture_output=True)
            git('init'); git('config','user.email','test@local'); git('config','user.name','test')
            (root/'source').write_text('old\n'); (root/'tests').mkdir(); (root/'tests/t').write_text('old\n')
            git('add','.'); git('commit','-m','base')
            (root/'untracked').write_text('new\n'); (root/'tests/t').write_text('test\n')
            self.assertFalse(capture_filtered_patch(root).strip())
            (root/'source').write_text('new\n')
            expected = oracle()['remove_patches_to_tests'](git('--no-pager','diff','--ignore-submodules=all').stdout.decode() + '\n')
            self.assertEqual(capture_filtered_patch(root), expected)
            git('add','source'); self.assertFalse(capture_filtered_patch(root).strip())
            git('commit','-m','staged'); self.assertFalse(capture_filtered_patch(root).strip())

    def test_all_missing_usage_skips_tools_but_still_caps_and_closes_session(self):
        self.compare([response(('task_done','{}'),usage=None) for _ in range(50)], patches=['\n'])

    def test_cap_empty_patch_and_test_only_patch(self):
        for patch in ['\n','diff --git a/tests/t b/tests/t\n+new\n']:
            actual=self.compare([response(content='text') for _ in range(50)],patches=[patch])
            self.assertEqual(actual[2],'turn_capped')
            self.assertFalse(actual[3].strip())
