import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from openhands_adapter.compat.trae_contract import MessageManager, SYS_PROMPT, TOOLS
from openhands_adapter.config import OpenHandsConfig, RunConfig, WorkflowConfig
from openhands_adapter.input_loader import load_case, InputLoadError
from openhands_adapter.openhands.prompts import build_user_prompt
from test_input_loader import _write_case
from trae_reference import assert_reference_hashes, oracle, response


class PromptTests(unittest.TestCase):
    def setUp(self):
        assert_reference_hashes()
        self.ref = oracle()

    def test_exact_system_user_and_schema(self):
        self.assertEqual(SYS_PROMPT, self.ref["SYS_PROMPT"])
        self.assertEqual(TOOLS, self.ref["TOOLS"])
        issue = "\n  báo lỗi\t\nnext line\n"
        prompt = build_user_prompt(Path("/testbed"), problem_statement=issue)
        self.assertEqual(prompt, self.ref["INIT_USER_PROMPT"].format(project_path="/testbed", issue=issue))
        self.assertEqual([t['function']['name'] for t in TOOLS], ['str_replace_editor', 'bash', 'task_done', 'think'])
        self.assertNotIn('required', TOOLS[1]['function']['parameters'])

    def test_formatter_profiles_whitespace_reminders_and_cache(self):
        for cap, reminder in [(50, True), (100, False)]:
            a = MessageManager('/testbed', 'issue\n', None, {}, reminder, cap, TOOLS)
            b = self.ref['MessageManager']('/testbed', 'issue\n', None, {}, reminder, cap, self.ref['TOOLS'])
            self.assertEqual(a.format_messages(), b.format_messages())
            for answer, tail in [
                (response(content='text')[0], []),
                (response(('think', ' { "thought" : " hi " } '))[0], [{'role': 'tool', 'content': 'Continue.', 'tool_call_id': 'call-0', 'agent_caller': ('think', {'thought': ' hi '})}]),
                (response(content='again')[0], [{'role': 'user', 'content': 'feedback\n'}]),
            ]:
                a.push_step(answer, tail); b.push_step(answer, tail)
                before = json.dumps(a.steps)
                self.assertEqual(a.format_messages(), b.format_messages())
                self.assertEqual(json.dumps(a.steps), before)

    def test_mapping_prefers_problem_statement_and_preserves_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); _write_case(root, 'case')
            case = load_case(root)
            self.assertEqual(case.issue_source, 'failure_log')
            self.assertEqual(case.problem_statement, case.failure_log.read_bytes().decode('utf-8'))
            data = json.loads(case.config_path.read_text()); data['problem_statement'] = '\n  Unicode lỗi\t\n'
            case.config_path.write_text(json.dumps(data))
            case = load_case(root)
            self.assertEqual(case.issue_source, 'problem_statement')
            self.assertEqual(case.problem_statement, data['problem_statement'])
            self.assertEqual(case.issue_sha256, hashlib.sha256(case.problem_statement.encode()).hexdigest())
            data['problem_statement'] = ' \n'; case.config_path.write_text(json.dumps(data))
            with self.assertRaises(InputLoadError): load_case(root)

    def test_missing_issue_and_overrides_fail_explicitly(self):
        for issue in [None, '', ' \n']:
            with self.assertRaises(ValueError): build_user_prompt(Path('/testbed'), problem_statement=issue)
        with self.assertRaises(ValueError): build_user_prompt(Path('/testbed'), problem_statement='issue', instructions='custom')
        for field in ['prompt', 'prompt_file']:
            with self.assertRaises(ValueError): RunConfig.from_mapping({field: 'custom'})
        self.assertEqual(OpenHandsConfig().scheduling_limit, 51)
        self.assertEqual(OpenHandsConfig(reference_profile='trae_multiswe').scheduling_limit, 101)
        self.assertIsNone(WorkflowConfig.from_mapping({'agent_timeout_seconds': None}).agent_timeout_seconds)
        with self.assertRaises(ValueError): OpenHandsConfig.from_mapping({'max_iterations': 50})
