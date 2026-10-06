from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import httpx
from openhands_adapter.compat.trae_llm_policy import send_with_reference_retry
from trae_reference import wrapper_oracle
from trae_transport_fixture import capture_transport, completion


class RetryTests(unittest.TestCase):
    def test_retry_reports_original_exception_before_sleep(self):
        error = ValueError('normalization failed')
        observed = []
        calls = 0

        def attempt():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise error
            return 'ok'

        result = send_with_reference_retry(
            attempt, lambda delay: observed.append(('sleep', delay)),
            on_error=lambda exc, number, delay: observed.append((exc, number, delay)))
        self.assertEqual(result, 'ok')
        self.assertEqual(observed, [(error, 1, 1), ('sleep', 1)])

    def test_retry_sequences_and_normalization_match_source(self):
        for failures in (0, 3, 12):
            for kind in ('exception', 'none', 'normalization'):
                with self.subTest(failures=failures, kind=kind):
                    expected_sleeps, actual_sleeps = [], []
                    expected_calls, actual_calls = [], []
                    def factory(calls):
                        def create(**kw):
                            calls.append(kw)
                            if len(calls) <= failures:
                                if kind == 'exception': raise ValueError('provider error')
                                if kind == 'none': return None
                                return SimpleNamespace(model_dump=lambda: (_ for _ in ()).throw(ValueError('normalize')))
                            return SimpleNamespace(model_dump=lambda: {'success': True})
                        return create
                    oracle_create = factory(expected_calls)
                    expected = wrapper_oracle(oracle_create, expected_sleeps.append)('gpt-5-mini-test', [], [], {'n': 1})
                    create = factory(actual_calls)
                    def attempt():
                        value = create()
                        return None if value is None else value.model_dump()
                    result = send_with_reference_retry(attempt, actual_sleeps.append)
                    self.assertEqual(result, expected)
                    self.assertEqual(actual_sleeps, expected_sleeps)
                    self.assertEqual(len(actual_calls), len(expected_calls))
                    if failures == 12:
                        self.assertEqual(actual_sleeps, [2**i for i in range(12)])
                        self.assertEqual(sum(actual_sleeps), 4095)

    def test_http_errors_have_exactly_twelve_attempts_without_inner_retries(self):
        for status in (429, 500, 'connection'):
            def handler(request):
                if status == 'connection': raise httpx.ConnectError('offline', request=request)
                return httpx.Response(status, json={'error': {'message': 'scripted failure', 'type': 'test'}})
            with self.subTest(status=status), capture_transport('repair', 'claude4-sonnet', handler=handler) as (transport, seen, sleeps, clients):
                with self.assertRaisesRegex(RuntimeError, '^no response from api$') as raised:
                    transport([{'role': 'user', 'content': 'same'}], [])
                self.assertIsNotNone(raised.exception.__cause__)
                self.assertEqual(len(seen), 12)
                self.assertTrue(all(body == seen[0] for body in seen))
                self.assertEqual(sleeps, [2**i for i in range(12)])
                self.assertEqual(clients[0]['max_retries'], 0)

    def test_success_after_errors_and_no_completed_response_cache(self):
        counter = 0
        def handler(request):
            nonlocal counter
            counter += 1
            return httpx.Response(500, json={'error': {'message': 'retry'}}) if counter <= 2 else httpx.Response(200, json=completion())
        with capture_transport('compression', 'gpt-5-mini-test', handler=handler) as (transport, seen, sleeps, clients):
            first = transport([{'role': 'user', 'content': 'same'}], [])
            second = transport([{'role': 'user', 'content': 'same'}], [])
            self.assertEqual(first, second)
            self.assertEqual(len(seen), 4)
            self.assertEqual(sleeps, [1, 2])

    def test_telemetry_failure_never_retries_successful_generation(self):
        with capture_transport('repair', 'claude4-sonnet') as (transport, seen, sleeps, clients):
            transport.llm._telemetry = Mock()
            transport.llm._telemetry.on_response.side_effect = RuntimeError('logging failed')
            with self.assertRaisesRegex(RuntimeError, 'logging failed'):
                transport([{'role': 'user', 'content': 'same'}], [])
            self.assertEqual(len(seen), 1)
            self.assertEqual(sleeps, [])
