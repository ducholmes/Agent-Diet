"""D13 runtime invariants tested through real SDK conversations."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from openhands.sdk import LLM
from openhands_adapter.config import RunConfig, OpenHandsConfig, AgentDietConfig, WorkflowConfig
from openhands_adapter.compat.trae_llm_policy import validate_trae_run
from openhands_adapter.openhands.agent import build_agent
from openhands_adapter.openhands.container import RepairContainer
from openhands_adapter.openhands.trae_agent import TraeContractAgent, TraeLocalConversation
from openhands_adapter.compat.trae_contract import TOOLS, SYS_PROMPT
from test_trae_contract_turns import Sandbox, ScriptedTransport
from trae_reference import response
from trae_transport_fixture import CAPABILITIES


class SDKControlTests(unittest.TestCase):
    def test_C13_01_02_06_ambient_files_and_stock_controls_have_no_effect(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for file in ('SOUL.md', 'AGENTS.md', '.openhands/skills/a/SKILL.md', '.openhands/hooks.json',
                         '.openhands/agents/a.md', '.openhands/mcp.json', '.openhands/plugins/a/manifest.json'):
                p=root/file;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('FORBIDDEN AMBIENT CONTEXT')
            llm=LLM(model='openai/test',api_key='fake')
            with patch('openhands_adapter.openhands.agent.build_llm',return_value=llm), \
                 patch('openhands_adapter.openhands.trae_session.stage_tools'), \
                 patch('openhands_adapter.openhands.trae_session.TraeSandbox',return_value=Sandbox()):
                agent,_=build_agent(RepairContainer('fixture',root,'image'),
                    OpenHandsConfig(auth='api-key',trae_capabilities=CAPABILITIES), AgentDietConfig(mode='skip'), WorkflowConfig(),execution_plan={})
            self.assertEqual(agent.tool_concurrency_limit,1);self.assertIsNone(agent.critic);self.assertIsNone(agent.condenser)
            transport=ScriptedTransport([response(content='plain text'),response(('task_done','{}'))]);agent._transport=transport
            c=TraeLocalConversation(agent=agent,workspace=root,visualizer=None,max_iteration_per_run=51,
                stuck_detection=False,hook_config=None,max_budget_per_run=None)
            try:
                c.send_message('issue')
                # A disabled budget returns before reading cost, including huge usage.
                with patch.object(type(c.conversation_stats), 'get_combined_metrics', side_effect=AssertionError('budget read cost')):
                    self.assertIsNone(c._budget_exceeded_detail())
                with patch('openhands.sdk.agent.agent.Agent.step',side_effect=AssertionError('stock step used')):
                    c.run()
                self.assertEqual(len(transport.requests),2)
                self.assertEqual(transport.requests[0][0][0],{'role':'system','content':SYS_PROMPT})
                self.assertEqual(transport.requests[0][1],TOOLS);self.assertIsNone(c._hook_processor);self.assertIsNone(c._stuck_detector)
                self.assertIsNone(c.max_budget_per_run);self.assertEqual(agent.tools_map,{})
                self.assertTrue((root/'SOUL.md').is_file())
            finally:c.close()

    def test_C13_09_12_watchdog_paths_controls_and_roundtrip(self):
        oh=OpenHandsConfig(auth='api-key',trae_capabilities=CAPABILITIES)
        diet=AgentDietConfig(mode='skip')
        for transport in (oh, OpenHandsConfig(auth='subscription', transport_conformance='adapted')):
            for seconds in (None, 1, 1800):
                workflow = WorkflowConfig(agent_timeout_seconds=seconds)
                validate_trae_run(transport, diet, workflow)
                run = RunConfig(openhands=transport, agentdiet=diet, workflow=workflow)
                run.validate()
                self.assertEqual(RunConfig.from_mapping(run.to_dict()).workflow.agent_timeout_seconds, seconds)
        for block, factory in (('openhands',OpenHandsConfig),('agentdiet',AgentDietConfig),('workflow',WorkflowConfig)):
            for field in ('fallback_model','custom_recovery','parallel_tools','extra_summarizer'):
                with self.subTest(block=block,field=field),self.assertRaisesRegex(ValueError,'Unsupported'):
                    factory.from_mapping({field:'unsupported'})
        for field in ('raw_events_path','response_path'):
            with self.assertRaisesRegex(ValueError,'unsupported'):RunConfig.from_mapping({field:'/tmp/custom'})
        config=RunConfig(openhands=oh,agentdiet=diet)
        self.assertEqual(RunConfig.from_mapping(config.to_dict()).to_dict(),config.to_dict())
