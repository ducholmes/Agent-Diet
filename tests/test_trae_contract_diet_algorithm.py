from copy import deepcopy
from types import SimpleNamespace
import random
import unittest
from unittest.mock import Mock, patch
from openhands_adapter.config import AgentDietConfig, RunConfig
from openhands_adapter.compat.trae_diet import count_token, validate_exact_diet
from diet_contract_fixture import managers, analyzer, assert_parity


class DietAlgorithmTests(unittest.TestCase):
    def test_A01_readiness_default_extended_no_backfill(self):
        for before, after in ((1,2),(3,2),(0,0),(1,0)):
            config=AgentDietConfig(ctx_before=before,ctx_after=after)
            actual,ref=managers(0)
            ns,calls,c,transport=analyzer(config)
            for i in range(7):
                for mgr in (actual,ref): mgr.push_step({'role':'assistant','content':'code line\n'*900},[])
                ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
                assert_parity(self,actual,ref,c)
                self.assertEqual(transport.requests,[r[1] for r in calls])
                if i+1 < before+after: self.assertEqual(c.metrics['seen_tokens'],0)

    def test_A02_A03_threshold_boundaries_and_no_replay(self):
        for delta in (-1,0,1):
            actual,ref=managers()
            n=count_token(actual.extract_step_into_traj(0))
            ns,_,c,t=analyzer(AgentDietConfig(threshold_tokens=n+delta))
            ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
            assert_parity(self,actual,ref,c)
            before=deepcopy(c.metrics); c.after_normal_turn(actual)
            self.assertEqual(c.metrics,before)
            self.assertEqual(len(t.requests),int(delta<=0))
            self.assertEqual(c.metrics['seen_tokens'],n)

    def test_A04_bypass_acceptance_operand_real_tokenizer(self):
        actual,ref=managers()
        gate=count_token(actual.extract_step_into_traj(0))
        bypass=count_token(actual.extract_step_into_traj(0,True))
        self.assertNotEqual(actual.extract_step_into_traj(0),actual.extract_step_into_traj(0,True))
        ns,_,c,_=analyzer()
        ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
        assert_parity(self,actual,ref,c)
        self.assertEqual(c.metrics['seen_tokens'],gate)
        self.assertEqual(c.metrics['erase_in_tokens'],bypass)

    def test_A04_distinct_gate_and_acceptance_counts(self):
        actual,ref=managers()
        ns,_,c,_=analyzer()
        counter=lambda text: 3000 if '<talk>' in text else (1000 if '<think>' in text else 2)
        ns['count_token']=counter
        ns['maybe_perform_analysis_step'](ref)
        with patch('openhands_adapter.compat.trae_diet.count_token',side_effect=counter): c.after_normal_turn(actual)
        assert_parity(self,actual,ref,c)
        self.assertEqual(c.metrics['seen_tokens'],1000)
        self.assertEqual(c.metrics['erase_in_tokens'],3000)

    def test_A05_special_token_errors_default_encoder(self):
        for text in ('code <>& 中文🙂\n','<|endoftext|>'):
            actual,ref=managers(text=text)
            ns,_,c,_=analyzer(AgentDietConfig(threshold_tokens=0))
            if 'endoftext' in text:
                with self.assertRaises(ValueError): ns['maybe_perform_analysis_step'](ref)
                with self.assertRaises(ValueError): c.after_normal_turn(actual)
            else:
                ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
            assert_parity(self,actual,ref,c)

    def test_A06_A07_lz4_real_utf8_and_zero_future_literal(self):
        for before,after in ((1,2),(1,0),(0,0)):
            for threshold in (0,500,10000):
                for text in ('Tiếng Việt\n'*600, 'a b c d e f g\n'*600):
                    actual,ref=managers(3,text=text)
                    ns,_,c,_=analyzer(AgentDietConfig(ctx_before=before,ctx_after=after,use_lz4=True,threshold_tokens=threshold))
                    ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
                    assert_parity(self,actual,ref,c)

    def test_A07_lz4_negative_marginal_and_equal_threshold(self):
        for x1,x2 in ((100,80),(100,100),(100,101)):
            actual,ref=managers(); n=count_token(actual.extract_step_into_traj(0))
            config=AgentDietConfig(threshold_tokens=n,use_lz4=True)
            ns,_,c,_=analyzer(config)
            ns['count_comp']=Mock(side_effect=[x1,x2])
            ns['maybe_perform_analysis_step'](ref)
            with patch('lz4.frame.compress',side_effect=[bytes(x1),bytes(x2)]): c.after_normal_turn(actual)
            assert_parity(self,actual,ref,c)

    def test_A08_A10_acceptance_strict_boundaries_whitespace(self):
        # Controlled counts prove operators separately from real tokenizer fixtures.
        for old,new,expected in ((2500,2101,False),(2500,2100,True),
                                 (1000,799,True),(1000,800,False),(1000,801,False)):
            actual,ref=managers()
            output='   boundary content   '
            ns,_,c,_=analyzer(answers=([{'content':output+'</step>'}],['length'],{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}))
            counter=lambda text: new if text==output else old
            ns['count_token']=counter
            ns['maybe_perform_analysis_step'](ref)
            with patch('openhands_adapter.compat.trae_diet.count_token',side_effect=counter): c.after_normal_turn(actual)
            assert_parity(self,actual,ref,c)
            self.assertEqual(c.metrics['erase_count'],int(expected))

    def test_A11_counters_each_branch(self):
        for text,reason,completion in (('short</step>','stop',None),('short','length',3),
                                      ('line Unicode Tiếng Việt <think> agent\n'*10000+'</step>','stop',3),
                                      ('short</step>','stop',3)):
            actual,ref=managers()
            ns,_,c,_=analyzer(answers=([{'content':text}],[reason],{'total_tokens':13,'prompt_tokens':10,'completion_tokens':completion}))
            ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
            assert_parity(self,actual,ref,c)
            self.assertEqual(c.metrics['analysis_count'],1)
            self.assertEqual(c.metrics['compression_total_tokens'],0 if completion is None else 13)

    def test_A12_disabled_skip_missing_compressor(self):
        for config in (AgentDietConfig(enabled=False),AgentDietConfig(mode='skip')):
            actual,_=managers(); _,_,c,t=analyzer(config)
            c.after_normal_turn(actual)
            self.assertEqual(c.metrics['analysis_count'],0); self.assertEqual(t.requests,[])
        actual,_=managers(); _,_,c,_=analyzer(); c.compressor=None
        with self.assertRaisesRegex(ValueError,'requires a compressor'): c.after_normal_turn(actual)
        self.assertEqual(c.metrics['analysis_count'],1)

    def test_A13_A17_config_reject_exact_preserve_generic_roundtrip(self):
        for field,value in (('minimum_reduction_tokens',0),('minimum_reduction_ratio',0),('ctx_before',0)):
            raw={'openhands':{'auth':'api-key'},'agentdiet':{field:value}}
            with self.assertRaisesRegex(ValueError,field): RunConfig.from_mapping(raw)
            raw['openhands']['reference_profile']='generic'
            cfg=RunConfig.from_mapping(raw)
            self.assertEqual(getattr(cfg.agentdiet,field),value)
            self.assertEqual(RunConfig.from_mapping(cfg.to_dict()).to_dict(),cfg.to_dict())
        validate_exact_diet(AgentDietConfig(ctx_before=0,ctx_after=0,use_lz4=True))

    def test_A13_cli_env_overrides_and_worker_config_roundtrip(self):
        from openhands_adapter.cli import build_parser, _override
        for flag,value,name in (('--min-reduction-tokens','0','minimum_reduction_tokens'),
                                ('--min-reduction-ratio','0','minimum_reduction_ratio'),
                                ('--ctx-before','0','ctx_before')):
            config=RunConfig()
            args=build_parser().parse_args(['--model','openai/test',flag,value])
            with self.assertRaisesRegex(ValueError,name): _override(config,args,None)
        with patch.dict('os.environ',{'AGENTDIET_MIN_REDUCTION_TOKENS':'1'}):
            with self.assertRaisesRegex(ValueError,'minimum_reduction_tokens'): RunConfig.from_env()
        cfg=RunConfig.from_mapping({'agentdiet':{'compressor_model':'inherit','show_ctx':False,'use_lz4':True,'ctx_after':0}})
        self.assertEqual(RunConfig.from_mapping(cfg.to_dict()).to_dict(),cfg.to_dict())

    def test_A14_A15_random_unicode_metric_is_remaining_ids(self):
        for ratio in (.25,1):
            actual,ref=managers(text='🙂 漢字 Tiếng Việt é\n'*400)
            rng=random.Random(83)
            ns,_,c,_=analyzer(AgentDietConfig(mode='random',lingua_ratio=ratio),rng=rng)
            ns['maybe_perform_analysis_step'](ref)
            with patch('openhands_adapter.compat.trae_diet.random',random.Random(83)): c.after_normal_turn(actual)
            assert_parity(self,actual,ref,c)
            if ratio==1: self.assertEqual(c.metrics['erase_count'],1)

    def test_A16_lingua_kwargs_growth_errors_no_fallback(self):
        for output in ('long text\n'*10000, RuntimeError('lingua model unavailable')):
            fake=Mock()
            if isinstance(output,Exception): fake.compress_prompt.side_effect=output
            else: fake.compress_prompt.return_value={'compressed_prompt':output}
            actual,ref=managers()
            ns,_,c,_=analyzer(AgentDietConfig(mode='lingua'),lingua=fake)
            with patch('openhands_adapter.diet.strategies._lingua_compressor',return_value=fake):
                if isinstance(output,Exception):
                    with self.assertRaisesRegex(RuntimeError,'unavailable'): ns['maybe_perform_analysis_step'](ref)
                    with self.assertRaisesRegex(RuntimeError,'unavailable'): c.after_normal_turn(actual)
                else:
                    ns['maybe_perform_analysis_step'](ref); c.after_normal_turn(actual)
            assert_parity(self,actual,ref,c)
            args=fake.compress_prompt.call_args.kwargs
            self.assertEqual(args,{'rate':.25,'force_tokens':['\n','?']})

    def test_A16_lingua_model_cpu_constructor(self):
        from openhands_adapter.diet.strategies import _lingua_compressor
        fake=Mock()
        _lingua_compressor.cache_clear()
        with patch.dict('sys.modules',{'llmlingua':SimpleNamespace(PromptCompressor=fake)}):
            _lingua_compressor()
        fake.assert_called_once_with(model_name='microsoft/llmlingua-2-xlm-roberta-large-meetingbank',use_llmlingua2=True,device_map='cpu')
        _lingua_compressor.cache_clear()
