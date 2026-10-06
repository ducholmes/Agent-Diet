from copy import deepcopy
import unittest
from openhands_adapter.compat.trae_llm_policy import TraeLLMPolicy
from openhands_adapter.diet.prompts import build_compression_window
from trae_reference import compression_oracle
from diet_contract_fixture import managers, analyzer, assert_parity


class DietSerializationTests(unittest.TestCase):
    def test_S01_initial_empty_ids(self):
        actual, ref = managers(0)
        self.assertEqual(actual.extract_step_into_traj(-1), ref.extract_step_into_traj(-1))
        for i in range(3):
            for m in (actual, ref): m.push_step({'role':'assistant','content':''}, [])
            self.assertEqual(actual.extract_step_into_traj(i), ref.extract_step_into_traj(i))
            self.assertEqual(actual.extract_step_into_traj(i), f'<step id="{i}">\n</step>')
        self.assertEqual(actual.count_turn(), 3)

    def test_S02_S06_raw_batch_literals_reasoning_thought(self):
        actual, ref = managers()
        msg = {'role':'assistant','content':'  visible <think> agent &<>\n',
               'reasoning_content':'hidden secret','tool_calls':[
            {'id':'b','function':{'name':'bash','arguments':'  { "z": "Tiếng Việt <>&",\n "a": 2 } \n'}},
            {'id':'a','function':{'name':'think','arguments':' {"thought":"  x\\n  y "} '}}]}
        follow = [{'role':'tool','content':'literal\n  output','tool_call_id':'b','agent_caller':('bash',{})},
                  {'role':'tool','content':'Continue.','tool_call_id':'a','agent_caller':('think',{'thought':'  x\n  y '})},
                  {'role':'user','content':' keep whitespace\n'}]
        for mgr in (actual,ref): mgr.steps[0] = [deepcopy(msg), *deepcopy(follow)]
        for bypass in (False, True):
            out = actual.extract_step_into_traj(0,bypass)
            self.assertEqual(out.encode(),ref.extract_step_into_traj(0,bypass).encode())
            self.assertNotIn('hidden secret',out)
            self.assertEqual(out.count('visible'),1)
            self.assertIn('{ "z": "Tiếng Việt <>&",\n "a": 2 }',out)
            self.assertIn('\nx\n  y\n',out)
            self.assertNotIn('Continue.',out)

    def test_S07_windows_hidden_keep_slots(self):
        actual,ref=managers(7)
        for before,after in ((1,2),(2,1),(0,0),(1,0)):
            for show in (True,False):
                for model in ('claude4-sonnet','gpt-5-mini-test'):
                    policy=TraeLLMPolicy('compression',model)
                    idx=actual.count_turn()-1-after
                    out=build_compression_window(actual,idx,policy,ctx_before=before,ctx_after=after,show_ctx=show)
                    _,(_,messages,_,_)=compression_oracle(ref,model,ctx_before=before,ctx_after=after,show_ctx=show)
                    self.assertEqual(out+'\n\nNow, compress the step '+str(idx)+'.',messages[1]['content'])

    def test_S08_S09_nested_original_two_compressions(self):
        for mode in ('ours','delete','random'):
            from openhands_adapter.config import AgentDietConfig
            from unittest.mock import patch
            actual,ref=managers()
            ns,_,c,_=analyzer(AgentDietConfig(mode=mode))
            for turn in (3,4):
                # A stable RNG sample makes random baseline source/adapter comparable.
                with patch('random.sample', side_effect=lambda seq,n:list(seq)[:n]):
                    ns['maybe_perform_analysis_step'](ref)
                    c.after_normal_turn(actual)
                assert_parity(self,actual,ref,c)
                self.assertIn('<talk>' if mode=='ours' else '<think>',actual.steps[turn-3][0]['agent_erased'])
                if turn==3:
                    for mgr in (actual,ref): mgr.push_step({'role':'assistant','content':'next'},[])
            self.assertIn('<step id="0">\n<step id="0">', actual.extract_step_into_traj(0))

    def test_S10_empty_whitespace_reminders_remove_batch(self):
        for output in ('summary','', '   '):
            actual,ref=managers()
            ns,_,c,_=analyzer(answers=([{'content':output+'</step>'}],['length'],{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}))
            ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
            assert_parity(self,actual,ref,c)
            self.assertEqual(len(actual.steps[0]),1)
            self.assertNotIn('tool_calls',actual.steps[0][0])
            self.assertFalse(any(k.startswith('agent_') for msg in actual.format_messages() for k in msg))

    def test_S11_last_target_continue_cache(self):
        from openhands_adapter.config import AgentDietConfig
        actual,ref=managers(1)
        ns,_,c,_=analyzer(AgentDietConfig(ctx_before=0,ctx_after=0))
        ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
        assert_parity(self,actual,ref,c)
        self.assertIn('Continue in standard tool call format.',actual.format_messages()[-1]['content'])

    def test_S12_invalid_content_caller_same_exception(self):
        for mutation in (lambda m:m.steps[0][0].update(content=None),
                         lambda m:m.steps[0][1].update(agent_caller=None),
                         lambda m:m.steps[0][1].update(agent_caller=('think',{'thought':None}))):
            actual,ref=managers(); mutation(actual); mutation(ref)
            ns,_,c,_=analyzer()
            with self.assertRaises(Exception) as err: ns['maybe_perform_analysis_step'](ref)
            with self.assertRaises(type(err.exception)): c.after_normal_turn(actual)
            assert_parity(self,actual,ref,c)
