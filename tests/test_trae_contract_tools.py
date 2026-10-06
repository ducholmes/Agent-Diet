from copy import deepcopy
import unittest
from openhands_adapter.openhands.trae_tools import parse_tool_response
from trae_reference import assert_reference_hashes, oracle, response


class Session:
    def __init__(self, outputs=None):
        self.outputs = iter(outputs or [])
        self.commands = []
    def execute(self, cmd):
        self.commands.append(cmd)
        return next(self.outputs)


class ToolTests(unittest.TestCase):
    def setUp(self): assert_reference_hashes()

    def test_raw_dispatch_matches_source(self):
        scenarios = [
            (response(('bash', '{ broken'))[0], []),
            (response(('unknown', '{}'), ('think', '{"thought":"x"}'), ('task_failed', 'null'))[0], []),
            (response(('task_done', ' { } '), ('think', ' { "thought" : "x" } '))[0], []),
            (response(('bash', '{"command":"echo hi", "ignored":true}'))[0], ['', 'Tool Call Status: 0\nhi\n']),
            (response(('str_replace_editor', '{"command":"view", "path":"/testbed/test_a", "view_range":[1, -1]}'))[0], ['', 'Tool Call Status: -1\nerror\n']),
            (response(('bash', '{"command":"no-output"}'))[0], ['', 'Tool Call Status: 0']),
            (response(('bash', '{"command":"timeout"}'))[0], ['Command timed out after 180 seconds. Partial output:\n + partial']),
            (response(('bash', '{}'))[0], ['', 'prefix\n Tool Call Status: 0\nbody\nTool Call Status: -1']),
        ]
        ref = oracle()['parse_tool_response']
        for answer, outputs in scenarios:
            with self.subTest(answer=answer):
                a, b = Session(outputs), Session(outputs)
                self.assertEqual(parse_tool_response(deepcopy(answer), '', a), ref(deepcopy(answer), '', b))
                self.assertEqual(a.commands, b.commands)

    def test_source_argument_type_failures_are_preserved(self):
        for arguments in ['null', '[]', '{"command":null}']:
            a = response(('bash', arguments))[0]
            def error(fn):
                try: fn(deepcopy(a), '', Session())
                except Exception as exc: return type(exc)
            self.assertEqual(error(parse_tool_response), error(oracle()['parse_tool_response']))
