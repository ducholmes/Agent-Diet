"""HTTP capture at the exact transport's pinned serializer boundary."""
import json
from contextlib import contextmanager
from unittest.mock import patch
import httpx
from openai import OpenAI
from openhands.sdk import LLM
from openhands_adapter.compat.trae_llm_policy import TraeLLMPolicy
from openhands_adapter.openhands.trae_transport import SDKRawTransport
from openhands_adapter.diet.core import DietMetrics
from openhands_adapter.token_tracking import install_token_tracking

CAPABILITIES = ('max_tokens', 'n', 'temperature', 'stop', 'reasoning_effort', 'tools', 'cache_control', 'assistant_prefill')


def completion(content='short</step>', *, calls=None, finish='stop'):
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    return {'id': 'test-response', 'object': 'chat.completion', 'created': 1, 'model': 'gpt-5.6-sol',
            'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}}


@contextmanager
def capture_transport(role, reference, *, handler=None, llm=None):
    seen, sleeps, clients = [], [], []
    llm = llm or LLM(model='openai/gpt-5.6-sol', api_key='fake', base_url='https://mock.invalid/v1', seed=None)
    install_token_tracking(llm, DietMetrics())
    transport = SDKRawTransport(llm, policy=TraeLLMPolicy(role, reference), capabilities=CAPABILITIES, sleep=sleeps.append)
    def route(request):
        seen.append(json.loads(request.content))
        return handler(request) if handler else httpx.Response(200, json=completion())
    def factory(**kwargs):
        clients.append(kwargs)
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(route)))
    with patch('openai.OpenAI', side_effect=factory):
        yield transport, seen, sleeps, clients
