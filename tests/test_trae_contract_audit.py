from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from openhands_adapter.events import configure, read_events
from openhands_adapter.compat.audit import verify_bundle, payload_hash, finalize_bundle, byte_hash
from audit_fixture import exercise
from trae_transport_fixture import capture_transport
from openhands_adapter.compat.trae_contract import TOOLS
from openhands_adapter.token_tracking import aggregate_calls
import httpx


class AuditContractTests(unittest.TestCase):
    def tearDown(self):configure(None)

    def test_A14_01_02_03_04_07_09_12_raw_hash_noninterference_sdk_archive_and_submission(self):
        with tempfile.TemporaryDirectory() as td:
            runs=[exercise(Path(td)/str(raw),raw=raw) for raw in (True,False)]
            a,b=runs
            # Only workspace paths in effective input can differ; these fixture
            # model bodies contain no paths, so every character compares directly.
            self.assertEqual(a[2:4],b[2:4]);self.assertEqual(len(a[3]),1)
            for result,output,requests,compressions,sandboxes in runs:
                self.assertEqual(result.agent,'task_done');self.assertFalse(result.resolved)
                self.assertEqual(result.patch_origin,'terminal_snapshot');self.assertEqual(len(requests),4)
                self.assertTrue(all(s.closed==1 for s in sandboxes))
                snapshot=json.loads((output/'contract-result.json').read_text())
                self.assertEqual(snapshot['patch'].encode(),(output/'patch.diff').read_bytes())
                self.assertEqual((output/'contract-patch.diff').read_bytes(),(output/'patch.diff').read_bytes())
                manifest=json.loads((output/'audit/manifest.json').read_text())
                self.assertEqual(manifest['effective_config_sha256'],payload_hash(manifest['effective_config']))
                self.assertEqual(manifest['runtime_controls']['tool_concurrency_limit'],1)
                self.assertIsNone(manifest['runtime_controls']['hooks'])
                events=read_events(output/'events.jsonl')
                telemetry=[e for e in events if e['type']=='llm_call_started']
                requests_audit=[e for e in events if e['type']=='trae_llm_request']
                self.assertEqual({e['call_id'] for e in telemetry},{e['request_id'] for e in requests_audit})
                self.assertEqual(aggregate_calls(events)['repair']['calls'],4)
                self.assertEqual(aggregate_calls(events)['compression']['calls'],1)
                self.assertEqual(verify_bundle(output)['status'],'pass')
                self.assertFalse((output/'.tmp').exists())
                self.assertTrue(any(e['type']=='diet_step_change' for e in events))
                self.assertEqual([e['logical_turn'] for e in events if e['type']=='trae_after_normal_turn'],[1,2,3])
                self.assertTrue(all(e['sdk_event_ids'] for e in events if e['type']=='trae_step_pushed'))
            raw_output,hash_output=a[1],b[1]
            self.assertTrue(list((raw_output/'audit/sdk').rglob('*.json')))
            self.assertFalse((hash_output/'audit/sdk').exists())
            self.assertIn('steps',json.loads((raw_output/'contract-result.json').read_text()))
            self.assertNotIn('steps',json.loads((hash_output/'contract-result.json').read_text()))
            self.assertNotIn('agent test',(hash_output/'contract-result.json').read_text())
            self.assertNotIn('original_patch',json.loads((hash_output/'contract-result.json').read_text()))
            self.assertNotIn('observed Unicode',(hash_output/'events.jsonl').read_text())
            self.assertEqual(verify_bundle(raw_output,full=True)['status'],'pass')
            self.assertEqual(verify_bundle(hash_output,full=True)['status'],'fail')

    def test_A14_10_17_generation_error_survives_export_without_next_repair(self):
        with tempfile.TemporaryDirectory() as td:
            result,output,requests,compressions,_=exercise(Path(td),fatal=True)
            self.assertEqual(len(requests),3);self.assertEqual(len(compressions),1)
            snapshot=json.loads((output/'contract-result.json').read_text())
            self.assertEqual(snapshot['gen'],'generation_error');self.assertEqual(snapshot['cause_type'],'AttributeError')
            self.assertEqual(result.agent,'generation_error');self.assertEqual(result.patch_origin,'error_wip')
            self.assertTrue((output/'recovery-patch.diff').is_file())
            self.assertEqual(verify_bundle(output)['status'],'fail')
        with tempfile.TemporaryDirectory() as td:
            result,output,requests,compressions,_=exercise(Path(td),audit_fault=True)
            self.assertEqual(result.agent,'task_done');self.assertEqual(len(requests),4);self.assertEqual(len(compressions),1)
            self.assertEqual(verify_bundle(output)['status'],'fail')

    def test_A14_18_credentials_are_sanitized_without_changing_actual_body(self):
        secret='sentinel-credential-abc123'
        with tempfile.TemporaryDirectory() as td:
            result,output,requests,_,_=exercise(Path(td),secret=secret)
            self.assertEqual(result.agent,'task_done')
            self.assertTrue(any(secret in json.dumps(p) for p in requests))
            for file in output.rglob('*'):
                if file.is_file():self.assertNotIn(secret,file.read_text(errors='replace'),str(file))
            report=verify_bundle(output,full=True)
            self.assertEqual(report['status'],'fail');self.assertTrue(any('redacted' in v for v in report['limitations']))

    def test_A14_06_retry_correlation_has_one_logical_request(self):
        with tempfile.TemporaryDirectory() as td:
            output=Path(td);configure(output/'events.jsonl',reset=True,run_id='r',case_id='c')
            def handler(request):return httpx.Response(500,json={'error':{'message':'fixture'}})
            with capture_transport('repair','claude4-sonnet',handler=handler) as (transport,seen,sleeps,_):
                self.assertRaisesRegex(RuntimeError,'no response from api',transport,[{'role':'user','content':'issue'}],TOOLS)
            events=read_events(output/'events.jsonl');request=[e for e in events if e['type']=='trae_llm_request'][0]
            attempts=[e for e in events if e['type']=='trae_transport_attempt']
            self.assertEqual(len(attempts),12);self.assertEqual({e['request_id'] for e in attempts},{request['request_id']})
            self.assertEqual({e['body_sha256'] for e in attempts},{request['payload_sha256']})
            self.assertEqual(aggregate_calls(events)['repair']['calls'],1)

    def test_A14_11_provider_null_usage_is_not_sdk_default_zero(self):
        from trae_transport_fixture import completion
        for usage in (None, {}, {'prompt_tokens': None, 'completion_tokens': None},
                      {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}):
            with self.subTest(usage=usage), tempfile.TemporaryDirectory() as td:
                configure(Path(td)/'events.jsonl',reset=True,run_id='r',case_id='c')
                def handler(request):
                    raw=completion();raw['usage']=usage
                    return httpx.Response(200,json=raw)
                with capture_transport('repair','claude4-sonnet',handler=handler) as (transport,*_):
                    _,_,reported=transport([{'role':'user','content':'issue'}],TOOLS)
                totals=aggregate_calls(read_events(Path(td)/'events.jsonl'))['repair']
                zero=bool(usage) and usage.get('prompt_tokens') == 0
                self.assertEqual(totals['reported_calls'],int(zero))
                self.assertEqual(totals['input_tokens'],0 if zero else None)
                if usage is None:
                    self.assertIsNone(reported)
                else:
                    # The frozen source boundary is OpenAI model_dump(), which
                    # includes absent optional usage fields as null.
                    self.assertEqual(reported['prompt_tokens'],usage.get('prompt_tokens'))
                    self.assertEqual(reported['completion_tokens'],usage.get('completion_tokens'))

    def test_A14_19_21_manifest_patch_payload_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            _,output,*_=exercise(Path(td))
            for name in ('audit/sdk/nested/manifest.json','logs/validation/check.log'):
                path=output/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{}\n')
            finalize_bundle(output,status='finished')
            for name in ('patch.diff','contract-manifest.json','events.jsonl',
                         'audit/sdk/nested/manifest.json','logs/validation/check.log'):
                path=output/name;old=path.read_bytes();path.write_bytes(old+b' ')
                self.assertEqual(verify_bundle(output)['status'],'fail',name);path.write_bytes(old)
            path=output/'audit/manifest.json';old=path.read_text();value=json.loads(old);value['run_id']='tampered'
            path.write_text(json.dumps(value));self.assertEqual(verify_bundle(output)['status'],'fail');path.write_text(old)
            value=json.loads(old);value['schema_version']=99;path.write_text(json.dumps(value))
            self.assertEqual(verify_bundle(output)['status'],'fail')
            for updates in ({'schema_version':True},{'implementation_sha256':{}},
                            {'sdk_source_sha256':{}},{'dependency_lock_sha256':'0'*64},
                            {'reference':{}},
                            {'runtime_controls':{**json.loads(old)['runtime_controls'],'tool_concurrency_limit':True}}):
                path.write_text(json.dumps({**json.loads(old),**updates}))
                self.assertEqual(verify_bundle(output)['status'],'fail',updates)

    def test_A14_11_23_pending_evidence_marks_bundle_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            _,output,*_=exercise(Path(td))
            path=output/'events.jsonl';original=path.read_bytes()
            records=[json.loads(line) for line in original.splitlines()]
            for removed in ('run_finished','worker_finished','llm_call_finished','trae_transport_result'):
                selected=[e for e in records if e['type'] != removed]
                path.write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in selected))
                finalize_bundle(output,status='finished')
                self.assertFalse(json.loads((output/'audit/manifest.json').read_text())['audit_complete'],removed)
                self.assertEqual(verify_bundle(output)['status'],'fail')
            path.write_bytes(original);finalize_bundle(output,status='finished')
            self.assertEqual(verify_bundle(output)['status'],'pass')

    def test_A14_20_source_oracle_full_history_with_allowed_model_difference(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from openai.types.chat import ChatCompletion
        from trae_reference import response, run_expert, diet_oracle, diet_metrics
        from test_trae_contract_turns import Sandbox
        from openhands_adapter.config import AgentDietConfig
        from openhands_adapter.compat.audit import comparison_projection
        scripts=[response(('bash','{"command":"prepare-fixture"}'),('think',' { "thought" : "keep raw" } '),content='visible talk\n'),
                 response(('bash','{"command":"fix-fixture"}'),content=''),
                 response(('think','{"thought":"done soon"}'),content=''),response(('task_done','{}'),content='done')]
        normalized=[]
        for answer,reason,_ in scripts:
            raw={'id':'fixture','object':'chat.completion','created':1,'model':'test',
                 'choices':[{'index':0,'message':answer,'finish_reason':reason}],
                 'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}}
            parsed=ChatCompletion.model_validate(raw).model_dump()
            normalized.append((parsed['choices'][0]['message'],reason,parsed['usage']))
        with tempfile.TemporaryDirectory() as td:
            _,output,*_=exercise(Path(td),script=scripts)
            snapshot=json.loads((output/'contract-result.json').read_text())
            config=AgentDietConfig()
            ns,calls=diet_oracle(config,reference_model='gpt-5-mini-2025-08-07',
                answers=([{'content':'short</step>'}],['stop'],{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}))
            metrics=diet_metrics()
            def hook(mgr):
                mgr.metrics=metrics;ns['maybe_perform_analysis_step'](mgr)
            sandbox=Sandbox(outputs=['','Tool Call Status: 0\n'+'observed Unicode lỗi\n'*2500,'','Tool Call Status: 0\nok\n'],
                            patches=[snapshot['original_patch']])
            with redirect_stdout(StringIO()):expected=run_expert(normalized,sandbox,issue='issue',hook=hook)
            evidence={'repair_requests':[{'model':'source-reference','messages':m,'tools':t,'max_tokens':8192,'n':1,'temperature':0.0} for m,t in expected[0]],
                      'compression_requests':[{'model':'source-reference','messages':call[1],'max_tokens':8192,'n':1,'reasoning_effort':'low'} for call in calls],
                      'steps':expected[1],
                      'allowed_differences':[{'category':'model','field':['model'],'reason':'explicit actual model mapping',
                                              'evidence':'protocol-manifest.json'}]}
            oracle_path=output/'audit/source-oracle.json';oracle_path.write_text(json.dumps(evidence,ensure_ascii=False))
            finalize_bundle(output,status='finished')
            report=verify_bundle(output,full=True,oracle_path=oracle_path)
            self.assertEqual(report['status'],'pass',report)
            value={'model':'actual','messages':[{'role':'user','content':'actual'}]}
            projected=comparison_projection(value,evidence['allowed_differences'])
            self.assertEqual(projected['messages'],value['messages'])
            with self.assertRaisesRegex(ValueError,'allowlist'):
                comparison_projection(value,[{'category':'harness','field':['messages'],'reason':'SDK defaults','evidence':'fixture'}])
            evidence['repair_requests'][0]['messages'][0]['content']+='tamper'
            oracle_path.write_text(json.dumps(evidence));finalize_bundle(output,status='finished')
            report=verify_bundle(output,full=True,oracle_path=oracle_path)
            self.assertEqual(report['status'],'fail');self.assertTrue(any('character' in e.get('field','') for e in report['errors']))

    def test_A14_05_11_shared_telemetry_context_isolated_across_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from openhands.sdk import LLM
        from litellm.types.utils import ModelResponse
        from openhands_adapter.compat.audit import request_context
        from openhands_adapter.token_tracking import install_token_tracking
        from openhands_adapter.diet.core import DietMetrics
        from trae_transport_fixture import completion
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'events.jsonl';configure(path,reset=True,run_id='r',case_id='c')
            llm=LLM(model='openai/test',api_key='fake');install_token_tracking(llm,DietMetrics())
            barrier=Barrier(2)
            def call(role):
                with request_context(role,force_new=True,exact_transport=True) as rid:
                    llm.telemetry.on_request({'messages':[{'role':'user','content':role}]})
                    barrier.wait(10)
                    llm.telemetry.on_response(ModelResponse(**completion()))
                    return rid
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(call,role) for role in ('repair','compression')]
                ids={f.result(timeout=20) for f in futures}
            events=read_events(path);totals=aggregate_calls(events)
            self.assertEqual(totals['repair']['reported_calls'],1);self.assertEqual(totals['compression']['reported_calls'],1)
            self.assertEqual({e['call_id'] for e in events if e['type']=='llm_call_finished'},ids)

    def test_A14_08_gate_parser_and_reduction_decisions_match_source(self):
        from diet_contract_fixture import managers, analyzer, assert_parity
        from openhands_adapter.config import AgentDietConfig
        fixtures=[
            (AgentDietConfig(threshold_tokens=1000000),None,'below_threshold'),
            (AgentDietConfig(),([{'content':'unused'}],['stop'],{'completion_tokens':None}),'completion_usage_none'),
            (AgentDietConfig(),([{'content':'no close'}],['length'],{'total_tokens':13,'prompt_tokens':10,'completion_tokens':3}),'missing_close_without_stop')]
        with tempfile.TemporaryDirectory() as td:
            for index,(config,answers,reason) in enumerate(fixtures):
                path=Path(td)/str(index);configure(path,reset=True)
                actual,reference=managers();ns,_,condenser,_=analyzer(config,answers=answers)
                ns['maybe_perform_analysis_step'](reference);condenser.after_normal_turn(actual)
                assert_parity(self,actual,reference,condenser)
                decisions=[e for e in read_events(path) if e['type']=='diet_decision']
                self.assertEqual(decisions[-1]['status'],'rejected');self.assertEqual(decisions[-1]['reason'],reason)
                self.assertEqual(decisions[-1]['logical_turn'],3);self.assertEqual(decisions[-1]['step_index'],0)
