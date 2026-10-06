"""Shared oracle harness: expected state always comes from frozen Trae."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from openhands_adapter.compat.trae_contract import MessageManager, TOOLS
from openhands_adapter.compat.trae_llm_policy import TraeLLMPolicy
from openhands_adapter.config import AgentDietConfig
from openhands_adapter.diet.condenser import AgentDietCondenser
from openhands_adapter.openhands.compressor import build_compressor
from trae_reference import oracle, diet_oracle, diet_metrics


def managers(steps=3, text='line Unicode Tiếng Việt <think> agent\n' * 350):
    actual = MessageManager('/testbed', 'initial user <think> agent', None, {}, True, 50, TOOLS)
    for i in range(steps):
        actual.push_step({'role': 'assistant', 'content': text if i < 2 else '',
            'reasoning_content': 'PRIVATE REASONING', 'tool_calls': [
            {'id': f'call-{i}', 'type': 'function', 'function': {
             'name': 'think', 'arguments': ' {"thought": "  Unicode thought\\n "} '}}]}, [
            {'role': 'tool', 'content': 'Continue.', 'tool_call_id': f'call-{i}',
             'agent_caller': ('think', {'thought': '  Unicode thought\n '})}])
    reference = oracle()['MessageManager']('/testbed', '', None, diet_metrics(), True, 50, TOOLS)
    reference.user_message = deepcopy(actual.user_message)
    reference.steps = deepcopy(actual.steps)
    return actual, reference


def analyzer(config=None, *, answers=None, reference_model='gpt-5-mini-test', encoding=None, rng=None, lingua=None):
    config = config or AgentDietConfig()
    ns, calls = diet_oracle(config, answers=answers, reference_model=reference_model,
                           encoding=encoding, rng=rng, lingua=lingua)
    policy = TraeLLMPolicy('compression', reference_model)
    class Transport:
        def __init__(self):
            self.policy = policy
            self.requests = []
        def __call__(self, messages, tools):
            self.requests.append(deepcopy(messages))
            if isinstance(answers, BaseException):
                raise answers
            a, reasons, usage = deepcopy(answers or ([{'content': 'short</step>'}], ['stop'],
                 {'total_tokens': 13, 'prompt_tokens': 10, 'completion_tokens': 3}))
            return a[0], reasons[0], usage
    transport = Transport()
    condenser = AgentDietCondenser(config, exact_trae=True, compression_policy=policy if config.mode=='ours' else None)
    compressor = build_compressor(None, policy=policy, transport=transport,
        on_usage=lambda usage: condenser.diet.metrics.record_analysis_usage(usage))
    condenser.compressor = compressor
    return ns, calls, condenser, transport


def assert_parity(test, actual, reference, condenser):
    test.assertEqual(actual.steps, reference.steps)
    test.assertEqual(actual.format_messages(), reference.format_messages())
    m = condenser.metrics
    aliases = {'analysis_cost_tokens': 'compression_total_tokens', 'erase_tot_count': 'erase_count'}
    for key, value in reference.metrics.items():
        test.assertEqual(m[aliases.get(key,key)], value, key)
