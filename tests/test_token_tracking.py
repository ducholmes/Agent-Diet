from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from openhands_adapter.diet.core import DietMetrics
from openhands_adapter.events import configure, read_events
from openhands_adapter.token_tracking import aggregate_calls, compression_call, install_token_tracking, response_tokens


class TokenTrackingTests(unittest.TestCase):
    def test_chat_and_responses_usage_preserve_unknown_details(self):
        for raw in [
            {'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_tokens_details': {'cached_tokens': 80}, 'completion_tokens_details': {'reasoning_tokens': 5}}},
            {'usage': {'input_tokens': 100, 'output_tokens': 20, 'input_tokens_details': {'cached_tokens': 80}, 'output_tokens_details': {'reasoning_tokens': 5}}},
        ]:
            with self.subTest(raw=raw):
                tokens = response_tokens(raw)
                self.assertEqual(tokens['total_tokens'], 120)
                self.assertEqual(tokens['cached_input_tokens'], 80)
                self.assertEqual(tokens['reasoning_tokens'], 5)
                self.assertIsNone(tokens['cache_write_tokens'])
        tokens = response_tokens({'usage': {'input_tokens': 100}})
        self.assertEqual(tokens['usage_status'], 'unknown')
        self.assertIsNone(tokens['output_tokens'])
        self.assertIsNone(tokens['total_tokens'])
        self.assertEqual(response_tokens({'usage': {'input_tokens': 0, 'output_tokens': 0}})['usage_status'], 'reported')

    def test_cache_write_details_and_legacy_aliases(self):
        for details_key in ('prompt_tokens_details', 'input_tokens_details'):
            for count in (0, 30):
                with self.subTest(details_key=details_key, count=count):
                    tokens = response_tokens({'usage': {
                        'input_tokens': 100, 'output_tokens': 20,
                        details_key: {'cache_write_tokens': count, 'cache_creation_tokens': 40},
                        'cache_creation_input_tokens': 50,
                    }})
                    self.assertEqual(tokens['cache_write_tokens'], count)
        for usage in (
            {'input_tokens_details': {'cache_creation_tokens': 40}},
            {'cache_creation_input_tokens': 40},
        ):
            with self.subTest(usage=usage):
                self.assertEqual(response_tokens({'usage': usage})['cache_write_tokens'], 40)
        for invalid in (None, -1, True, '30'):
            with self.subTest(invalid=invalid):
                self.assertIsNone(response_tokens({'usage': {
                    'input_tokens_details': {'cache_write_tokens': invalid},
                }})['cache_write_tokens'])

    def test_cache_write_aggregation_preserves_missing_usage(self):
        def record(call_id, details):
            return {
                'type': 'llm_call_finished', 'call_id': call_id, 'role': 'repair',
                **response_tokens({'usage': {
                    'input_tokens': 100, 'output_tokens': 20,
                    'input_tokens_details': details,
                }}),
            }
        records = [record('a', {'cache_write_tokens': 30}), record('b', {'cache_write_tokens': 0})]
        self.assertEqual(aggregate_calls(records)['repair']['cache_write_tokens'], 30)
        records.append(record('c', {}))
        totals = aggregate_calls(records)['repair']
        self.assertIsNone(totals['cache_write_tokens'])
        self.assertEqual(totals['known_tokens']['cache_write_tokens'], 30)

    def test_pending_and_duplicate_records_do_not_inflate_totals(self):
        started = {'type': 'llm_call_started', 'call_id': 'a', 'model': 'shared', 'role': 'repair', 'status': 'running', 'usage_status': 'unknown'}
        finished = {**started, 'type': 'llm_call_finished', 'status': 'responded', **response_tokens({'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})}
        pending = {**started, 'call_id': 'b', 'role': 'compression'}
        totals = aggregate_calls([started, finished, finished, pending])
        self.assertEqual(totals['total']['calls'], 2)
        self.assertEqual(totals['repair']['total_tokens'], 110)
        self.assertEqual(totals['compression']['pending_calls'], 1)
        self.assertIsNone(totals['total']['total_tokens'])
        self.assertEqual(totals['total']['known_tokens']['total_tokens'], 110)
        self.assertFalse(totals['total']['complete'])

    def test_real_sdk_telemetry_attributes_shared_llm_without_calculating_cost(self):
        from openhands.sdk import LLM
        from litellm import ModelResponse
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            configure(path, reset=True)
            self.addCleanup(configure, None)
            llm = LLM(model='openai/gpt-4o', api_key='test', num_retries=0)
            metrics = DietMetrics()
            install_token_tracking(llm, metrics)
            original = llm.telemetry
            install_token_tracking(llm, metrics)
            self.assertIs(llm.telemetry, original)
            with patch('openhands.sdk.llm.utils.telemetry.litellm_completion_cost', side_effect=AssertionError('must not calculate money')):
                llm.telemetry.on_request({'messages': [{'role': 'user', 'content': 'fix bug'}]})
                llm.telemetry.on_response(ModelResponse(id='repair', model='gpt-4o', choices=[{'message': {'role': 'assistant', 'content': 'done'}, 'finish_reason': 'stop'}], usage={'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}))
                with compression_call(7):
                    llm.telemetry.on_request({'messages': [{'role': 'user', 'content': 'compress'}]})
                    llm.telemetry.on_response(ModelResponse(id='compression', model='gpt-4o', choices=[{'message': {'role': 'assistant', 'content': 'short'}, 'finish_reason': 'length'}], usage={'prompt_tokens': 50, 'completion_tokens': 10, 'total_tokens': 60}))
            events = read_events(path)
            totals = aggregate_calls(events)
            self.assertEqual(totals['repair']['total_tokens'], 120)
            self.assertEqual(totals['compression']['total_tokens'], 60)
            self.assertEqual(totals['total']['total_tokens'], 180)
            self.assertEqual(metrics.compression_llm_calls, 1)
            self.assertEqual(events[-1]['finish_reason'], 'length')
            self.assertEqual(events[-1]['step_index'], 7)
            self.assertGreater(events[-1]['context_token_estimate'], 0)
            self.assertNotIn('cost', events[-1])
            self.assertEqual(llm.metrics.accumulated_cost, 0)

    def test_real_responses_api_usage_and_status_are_recorded(self):
        from openhands.sdk import LLM
        from litellm.types.llms.openai import ResponsesAPIResponse
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            configure(path, reset=True)
            self.addCleanup(configure, None)
            llm = LLM(model='openai/gpt-4o', api_key='test', num_retries=0)
            install_token_tracking(llm, DietMetrics())
            llm.telemetry.on_request({'input': [{'role': 'assistant', 'content': '(System reminder: compressed for better efficiency) short'}], 'instructions': 'fix bug'})
            llm.telemetry.on_response(ResponsesAPIResponse(
                id='responses-1', created_at=1, output=[], model='gpt-4o', status='completed',
                usage={'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
                       'input_tokens_details': {'cached_tokens': 80}, 'output_tokens_details': {'reasoning_tokens': 5}},
            ))
            records = read_events(path)
            self.assertEqual(records[-1]['response_status'], 'completed')
            self.assertIsNone(records[-1]['finish_reason'])
            self.assertEqual(records[-1]['cached_input_tokens'], 80)
            self.assertEqual(records[-1]['reasoning_tokens'], 5)
            self.assertGreater(records[-1]['reminder_message_token_estimate'], 0)
            self.assertEqual(aggregate_calls(records)['total']['total_tokens'], 120)

    def test_actual_sdk_compressor_request_is_scoped_to_compression(self):
        from openhands.sdk import LLM
        from litellm import ModelResponse
        from openhands_adapter.openhands.compressor import build_compressor
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            configure(path, reset=True)
            self.addCleanup(configure, None)
            llm = LLM(model='openai/gpt-4o', api_key='test', api_mode='chat', num_retries=0)
            metrics = DietMetrics()
            install_token_tracking(llm, metrics)
            raw = ModelResponse(id='comp-1', model='gpt-4o', choices=[{'message': {'role': 'assistant', 'content': '<step id="3">summary</step>'}, 'finish_reason': 'stop'}], usage={'prompt_tokens': 40, 'completion_tokens': 10, 'total_tokens': 50})
            with patch.object(LLM, '_transport_call', return_value=raw):
                output = build_compressor(llm).compress_step('long original', '<step id="3">long original</step>', step_index=3)
            self.assertEqual(output, 'summary')
            usage = aggregate_calls(read_events(path))
            self.assertEqual(usage['compression']['calls'], 1)
            self.assertEqual(usage['compression']['total_tokens'], 50)
            self.assertEqual(usage['repair']['calls'], 0)
            self.assertEqual(metrics.compression_llm_calls, 1)

    def test_retry_and_terminal_error_keep_unknown_usage(self):
        from openhands.sdk import LLM
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            configure(path, reset=True)
            self.addCleanup(configure, None)
            llm = LLM(model='openai/gpt-4o', api_key='test', num_retries=0)
            install_token_tracking(llm, DietMetrics())
            with compression_call(2):
                llm.telemetry.on_request({})
                llm.telemetry.on_request({})
                llm.telemetry.on_error(RuntimeError('network'))
            llm.telemetry.on_request({})
            records = read_events(path)
            totals = aggregate_calls(records)
            self.assertEqual(totals['compression']['failed_calls'], 2)
            self.assertEqual(totals['repair']['pending_calls'], 1)
            self.assertIsNone(totals['total']['total_tokens'])


if __name__ == '__main__':
    unittest.main()
