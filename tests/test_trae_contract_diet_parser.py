import unittest
from diet_contract_fixture import managers, analyzer, assert_parity


class DietParserTests(unittest.TestCase):
    def compare(self,content,reason='stop',usage=None,answer=None):
        actual,ref=managers()
        usage=usage if usage is not None else {'total_tokens':13,'prompt_tokens':10,'completion_tokens':3}
        answer=answer if answer is not None else {'content':content}
        ns,_,c,_=analyzer(answers=([answer],[reason],usage))
        error=None
        try: ns['maybe_perform_analysis_step'](ref)
        except Exception as exc: error=exc
        if error:
            with self.assertRaises(type(error)): c.after_normal_turn(actual)
        else: c.after_normal_turn(actual)
        assert_parity(self,actual,ref,c)
        return actual,c,error

    def test_P01_unknown_completion_skip_before_bad_content_usage(self):
        actual,c,error=self.compare(None,usage={'completion_tokens':None})
        self.assertIsNone(error)
        self.assertEqual(c.metrics['analysis_count'],1)
        self.assertEqual(c.metrics['compression_total_tokens'],0)
        self.assertEqual(c.metrics['erase_count'],0)

    def test_P02_usage_zero_missing_wrong_types_addition_order(self):
        for usage in ({'completion_tokens':0,'prompt_tokens':0,'total_tokens':0},
                      {'completion_tokens':0,'prompt_tokens':4,'total_tokens':0},
                      {'completion_tokens':3,'prompt_tokens':None,'total_tokens':13},
                      {'completion_tokens':3,'total_tokens':13},
                      {'completion_tokens':3,'prompt_tokens':4,'total_tokens':'wrong'},
                      {'completion_tokens':3,'prompt_tokens':4}, {},
                      {'completion_tokens':'wrong','prompt_tokens':4,'total_tokens':13}):
            with self.subTest(usage=usage): self.compare('short</step>',usage=usage)

    def test_P03_P07_partition_finish_reasons_wrong_nested_id(self):
        for content in ('prefix <step id="999">summary</step>trailing',
                        'summary</step>trailing', '<step id="0"><step id="1">inner</step></step>',
                        'summary','</step>','   </step>', '<step id="42"></step>'):
            for reason in ('stop','length','content_filter','tool_calls','error'):
                with self.subTest(content=content,reason=reason):
                    self.compare(content,reason,answer={'content':content,'tool_calls':[{'arbitrary':'call'}]})

    def test_P08_opening_offsets_199_200_close_19_20(self):
        for offset in (195,196,199,200):
            for close in (19,20):
                text='x'*offset+'<step'+'y'*close+'> summary</step>'
                with self.subTest(offset=offset,close=close): self.compare(text)

    def test_P09_P10_whitespace_first_closing_literal_code(self):
        for text in ('\n \t </step>trailing</step>', 'literal code <step literal>\nsummary</step>',
                     'prose prefix\n<step id="wrong">no validator</step>', '<step malformed\nsummary</step>'):
            self.compare(text)

    def test_P11_null_missing_content_fatal_not_skipped(self):
        for answer in ({'content':None},{}, {'content':23}):
            _,c,error=self.compare(None,answer=answer)
            self.assertIsNotNone(error)
            self.assertEqual(c.metrics['compression_total_tokens'],13)

    def test_P12_skip_vs_reject_metrics(self):
        _,unknown,_=self.compare(None,usage={'completion_tokens':None})
        _,skipped,_=self.compare('missing close','length')
        _,rejected,_=self.compare('huge\n'*20000+'</step>')
        self.assertEqual(unknown.metrics['compression_total_tokens'],0)
        self.assertEqual(skipped.metrics['compression_total_tokens'],13)
        self.assertEqual(rejected.metrics['compression_total_tokens'],13)
        self.assertIn('completion_usage_none',unknown.metrics['rejected'])
        self.assertIn('missing_close_without_stop',skipped.metrics['rejected'])
        self.assertIn('insufficient_reduction',rejected.metrics['rejected'])
