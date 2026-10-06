from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import httpx
from openhands.sdk import LLM
from openhands_adapter.config import AgentDietConfig, OpenHandsConfig, WorkflowConfig
from openhands_adapter.events import configure, read_events
from openhands_adapter.openhands.trae_agent import TraeContractAgent, TraeLocalConversation
from openhands_adapter.openhands.worker import run_worker
from openhands_adapter.openhands.container import RepairContainer
from openhands_adapter.token_tracking import aggregate_calls, summarize_diet
from trae_reference import run_expert, response, diet_metrics
from trae_transport_fixture import capture_transport, completion, CAPABILITIES
from test_trae_contract_turns import Sandbox, run_sdk, ScriptedTransport
from diet_contract_fixture import managers, analyzer, assert_parity


class DietIntegrationTests(unittest.TestCase):
    def tearDown(self): configure(None)

    def test_I01_I02_full_sdk_lifecycle_matches_expert(self):
        config=AgentDietConfig()
        ns,calls,c,_=analyzer(config)
        ref_metrics={}
        def oracle_hook(mgr):
            for k,v in diet_metrics().items(): mgr.metrics.setdefault(k,v)
            ns['maybe_perform_analysis_step'](mgr)
            ref_metrics.update({k:mgr.metrics[k] for k in diet_metrics()})
        script=[response(('think','{"thought":"next"}'),content='large code output\n'*500) for _ in range(5)]
        script.append(response(('task_done','{}'),content='done'))
        expected=run_expert(script,Sandbox(),hook=oracle_hook)
        actual=run_sdk(script,Sandbox(),hook=c.after_normal_turn)
        self.assertEqual(actual,expected)
        self.assertEqual(c.metrics['erase_count'],3)
        self.assertEqual(c.metrics['analysis_count'],3)
        self.assertIn('<step id="0">\n<step id="0">',calls[1][1][1]['content'])
        self.assertEqual(c.metrics['erase_in_tokens'],ref_metrics['erase_in_tokens'])

    def test_I03_empty_done_normal_success_no_final_cap_no_flush(self):
        for script,patches in (([response(('task_done','{}'),content='large\n'*700),
             response(('think','{"thought":"next"}'),content=''),
             response(('think','{"thought":"next"}'),content=''),
             response(('task_done','{}'),content='')], ['', 'diff --git a/a b/a\n']),
             ([response(('think','{"thought":"next"}'),content='large\n'*700) for _ in range(50)],['diff --git a/a b/a\n'])):
            ns,_,c,_=analyzer()
            def hook(mgr):
                for k,v in diet_metrics().items(): mgr.metrics.setdefault(k,v)
                ns['maybe_perform_analysis_step'](mgr)
            self.assertEqual(run_sdk(script,Sandbox(patches=patches),hook=c.after_normal_turn),
                             run_expert(script,Sandbox(patches=patches),hook=hook))
            self.assertEqual(c.metrics['analysis_count'],1 if len(script)==4 else 48)

    def test_I04_skipped_rejected_then_next_target_no_replay(self):
        for result in ('missing close','huge\n'*30000+'</step>'):
            actual,ref=managers()
            ns,calls,c,t=analyzer(answers=([{'content':result}],['length'],{'total_tokens':13,'prompt_tokens':10,'completion_tokens':3}))
            for i in range(2):
                ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
                before=deepcopy(c.metrics); c.after_normal_turn(actual)
                self.assertEqual(before,c.metrics)
                assert_parity(self,actual,ref,c)
                if i==0:
                    for mgr in (actual,ref): mgr.push_step({'role':'assistant','content':'next'},[])
            self.assertEqual(len(t.requests),2)
            self.assertIn('step 1.',t.requests[-1][1]['content'])

    def test_I05_worker_fatal_type_preserved_cleanup_metrics_no_next_repair(self):
        for answer in ({'content':None}, {'content':'short</step>'}):
            with tempfile.TemporaryDirectory() as td:
                root=Path(td)
                configure(root/'events.jsonl',reset=True)
                bad_usage={'total_tokens':13,'prompt_tokens':None,'completion_tokens':3} if answer['content'] else {'total_tokens':13,'prompt_tokens':10,'completion_tokens':3}
                _,_,c,_=analyzer(answers=([answer],['stop'],bad_usage))
                scripts=[response(('think','{"thought":"next"}'),content='large\n'*800) for _ in range(4)]
                transport=ScriptedTransport(scripts); transport.protocol='chat-completions'; sandbox=Sandbox()
                agent=TraeContractAgent(llm=LLM(model='openai/test',api_key='fake')).bind_runtime(
                    transport=transport,sandbox=sandbox,after_normal_turn=c.after_normal_turn)
                c.compressor.llm=agent.llm
                cfg=OpenHandsConfig(auth='api-key',model='openai/test',trae_capabilities=CAPABILITIES)
                diet=AgentDietConfig.from_mapping({'compressor_model':'inherit'})
                with patch('openhands_adapter.openhands.worker.build_agent',return_value=(agent,c)):
                    from openhands.sdk.conversation.exceptions import ConversationRunError
                    with self.assertRaises(ConversationRunError) as failure:
                        run_worker(root,'issue',root,execution_plan={},container=RepairContainer('fake',root,'image'),openhands=cfg,diet=diet,workflow=WorkflowConfig())
                self.assertIsInstance(failure.exception.__cause__,TypeError if answer['content'] else AttributeError)
                manifest=json.loads((root/'contract-manifest.json').read_text())
                self.assertEqual(manifest['diet_conformance']['D11']['status'],'verified_local_source_oracle')
                self.assertEqual(manifest['diet_effective_config']['minimum_reduction_tokens'],400)
                self.assertEqual(manifest['protocol']['provider_semantics_verified_by_adapter'],False)
                self.assertEqual(len(transport.requests),3)
                self.assertGreaterEqual(sandbox.closed,1)
                events=read_events(root/'events.jsonl')
                self.assertTrue(any(e.get('type')=='trae_generation_error' and e['phase']=='compression' for e in events))
                self.assertEqual([e for e in events if e['type']=='worker_finished'][-1]['status'],'failed')
                m=summarize_diet(events,aggregate_calls(events))
                self.assertEqual(m['analysis_count'],1)
                self.assertEqual(m['reference_diet_metrics']['compression_total_tokens'],13)
                self.assertEqual(m['erase_count'],0)

    def test_I05_provider_exhaustion_no_parser_retry_or_repair_continuation(self):
        import openhands_adapter.openhands.compressor as module
        actual,_=managers()
        _,_,c,_=analyzer()
        def handler(request): return httpx.Response(500,json={'error':{'message':'offline'}})
        with capture_transport('compression','gpt-5-mini-test',handler=handler) as (transport,seen,sleeps,_):
            c.compressor=module.build_compressor(transport.llm,policy=transport.policy,transport=transport,
                on_usage=c.diet.metrics.record_analysis_usage)
            with self.assertRaisesRegex(RuntimeError,'no response from api'): c.after_normal_turn(actual)
        self.assertEqual(len(seen),12); self.assertEqual(sleeps,[2**i for i in range(12)])
        self.assertEqual(c.metrics['analysis_count'],1); self.assertEqual(c.metrics['erase_count'],0)

    def test_I06_persist_reload_marker_original_counters(self):
        actual,ref=managers(); ns,_,c,_=analyzer()
        ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
        with tempfile.TemporaryDirectory() as td:
            agent=TraeContractAgent(llm=LLM(model='openai/test',api_key='fake')).bind_runtime(transport=lambda *a:None,sandbox=Sandbox())
            convo=TraeLocalConversation(agent=agent,workspace=td,visualizer=None,stuck_detection=False)
            try:
                agent._persist(convo.state,actual)
                payload=json.loads(json.dumps(convo.state.agent_state['trae_contract']))
                convo.state.agent_state={'trae_contract':payload}
                reloaded,_=agent._manager(convo.state)
                _,_,new_c,t=analyzer()
                new_c.after_normal_turn(reloaded)
                self.assertEqual(new_c.metrics,c.metrics)
                self.assertEqual(t.requests,[])
                self.assertEqual(json.dumps(reloaded.steps),json.dumps(actual.steps))
                self.assertEqual(reloaded.format_messages(),actual.format_messages())
                ref.steps=json.loads(json.dumps(ref.steps))
                for mgr in (reloaded,ref): mgr.push_step({'role':'assistant','content':'next'},[])
                ns['maybe_perform_analysis_step'](ref); new_c.after_normal_turn(reloaded)
                assert_parity(self,reloaded,ref,new_c)
            finally: convo.close()

    def test_P11_raw_empty_choices_fatal_once_boundary(self):
        def handler(request):
            raw=completion(); raw['choices']=[]
            return httpx.Response(200,json=raw)
        with capture_transport('compression','gpt-5-mini-test',handler=handler) as (transport,seen,sleeps,_):
            from openhands_adapter.openhands.compressor import build_compressor
            c=build_compressor(transport.llm,policy=transport.policy,transport=transport)
            with self.assertRaises(IndexError): c.compress_exact_result('context',step_index=0)
        self.assertEqual(len(seen),1); self.assertEqual(sleeps,[])

    def test_P01_P02_raw_usage_null_unknown_zero(self):
        for usage in (None, {'prompt_tokens':0,'completion_tokens':None,'total_tokens':0},
                      {'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}):
            def handler(request):
                raw=completion('short</step>'); raw['usage']=usage
                return httpx.Response(200,json=raw)
            with capture_transport('compression','gpt-5-mini-test',handler=handler) as (transport,seen,sleeps,_):
                from openhands_adapter.openhands.compressor import build_compressor
                c=build_compressor(transport.llm,policy=transport.policy,transport=transport)
                if usage is None:
                    with self.assertRaises(TypeError): c.compress_exact_result('context',step_index=0)
                else:
                    result=c.compress_exact_result('context',step_index=0)
                    self.assertEqual(result.status,'skipped' if usage['completion_tokens'] is None else 'parsed')
                    self.assertEqual(c._exact_metrics.compression_total_tokens,0)
            self.assertEqual(len(seen),1); self.assertEqual(sleeps,[])

    def test_reference_metrics_never_overwritten_by_provider_usage(self):
        reference={'seen_tokens':50,'analysis_count':1,'erase_count':0,'erase_in_tokens':0,
                   'erase_out_tokens':0,'compression_total_tokens':0,
                   'analysis_prompt_tokens':0,'analysis_completion_tokens':0,'rejected':{}}
        events=[{'type':'diet_reference_metrics','metrics':reference},
                {'type':'llm_call_finished','call_id':'x','role':'compression','status':'responded',
                 'usage_status':'reported','input_tokens':900,'output_tokens':300,'total_tokens':1200}]
        totals=aggregate_calls(events)
        result=summarize_diet(events,totals)
        self.assertEqual(result['analysis_prompt_tokens'],0)
        self.assertEqual(result['compression_total_tokens'],0)
        self.assertEqual(result['provider_compression_usage']['total_tokens'],1200)
        self.assertEqual(result['reference_diet_metrics']['analysis_count'],1)

    def test_P04_P06_multiple_choice_reasons_source_membership(self):
        def handler(request):
            raw=completion('short',finish='length')
            raw['choices'].append({'index':1,'message':{'role':'assistant','content':'unused'},'finish_reason':'stop'})
            return httpx.Response(200,json=raw)
        with capture_transport('compression','gpt-5-mini-test',handler=handler) as (transport,_,_,_):
            from openhands_adapter.openhands.compressor import build_compressor
            c=build_compressor(transport.llm,policy=transport.policy,transport=transport)
            result=c.compress_exact_result('context',step_index=0)
        self.assertEqual(result.status,'parsed'); self.assertEqual(result.content,'short')
