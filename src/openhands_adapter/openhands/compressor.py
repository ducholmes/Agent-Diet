"""Trae compression prompt and response protocol on the OpenHands LLM API."""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..diet.prompts import SYSTEM_PROMPT, build_compression_messages
from ..events import emit
from ..compat.audit import context_fields
from ..token_tracking import compression_call


def _value(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class LLMCompressor:
    def __init__(self, llm: Any, *, on_usage: Callable[[dict[str, int]], None] | None = None,
                 assistant_prefill: bool | None = None, transport=None, policy=None) -> None:
        self.transport, self.policy = transport, policy
        self.protocol = ('responses-step-wrapper-v1' if getattr(transport, 'conformance', None) == 'adapted'
                         else 'trae-prefill' if policy is not None else 'generic')
        if policy is not None:
            if transport is None or transport.policy != policy or (self.protocol == 'trae-prefill' and assistant_prefill is False):
                raise ValueError('compression: exact mode requires assistant prefill and a matching role transport')
            assistant_prefill = self.protocol == 'trae-prefill'
        self.llm, self.on_usage, self.assistant_prefill = llm, on_usage, assistant_prefill
        from ..diet.core import DietMetrics
        self._exact_metrics = DietMetrics()

    def __call__(self, text: str, context: str) -> str:
        for match in re.finditer(r'<step id="(\d+)">\n', context):
            if context.startswith(text + '\n</step>', match.end()):
                return self.compress_step(text, context, step_index=int(match[1]))
        return self.compress_step(text, f'<step id="0">\n{text}\n</step>\n{context}', step_index=0)

    def compress_step(self, text: str, context: str, *, step_index: int) -> str:
        if self.policy is not None:
            result = self.compress_exact_result(context, step_index=step_index)
            return result.content if result.status == 'parsed' else result
        from openhands.sdk import Message, TextContent

        responses_api = self.llm.uses_responses_api() is True
        if responses_api and self.assistant_prefill:
            raise ValueError('Assistant prefill requires a compatible completion endpoint')
        messages = [
            Message(role='system', content=[TextContent(text=SYSTEM_PROMPT)]),
            Message(role='user', content=[TextContent(text=(
                context + f'\n\nNow, compress the step {step_index}. '
                f'Return <step id="{step_index}"> followed by the compressed content and </step>.'
            ))]),
        ]
        if self.assistant_prefill:
            messages.append(Message(role='assistant', content=[TextContent(text=(
                f'Sure. Here is the compressed content of step {step_index}: <step id="{step_index}">'
            ))]))
        kwargs: dict[str, Any] = {}
        if not responses_api:
            # Feature metadata is reviewed with the pinned OpenHands SDK.
            if (self.llm._model_features().supports_stop_words is True
                    and self.llm.disable_stop_word is False):
                kwargs['stop'] = '</step>'
        request = self.llm.responses if responses_api else self.llm.completion
        with compression_call(step_index):
            response = request(messages=messages, tools=None, **kwargs)
        raw = response.raw_response
        raw_usage = _value(raw, 'usage')
        prompt_tokens = _value(raw_usage, 'prompt_tokens', _value(raw_usage, 'input_tokens'))
        completion_tokens = _value(raw_usage, 'completion_tokens', _value(raw_usage, 'output_tokens'))
        if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
            raise ValueError('Compressor response is missing token usage')
        usage = {
            'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens,
            'total_tokens': _value(raw_usage, 'total_tokens') or prompt_tokens + completion_tokens,
        }
        if self.on_usage is not None:
            self.on_usage(usage)
        emit('diet_compressor_usage', step_index=step_index, usage=usage)
        choices = _value(raw, 'choices', []) or []
        finish_reason = _value(choices[0], 'finish_reason') if choices else None
        status = _value(raw, 'status')
        if finish_reason in {'length', 'content_filter', 'tool_calls', 'function_call', 'error'} or status in {'incomplete', 'failed', 'cancelled'}:
            raise ValueError(f'Incomplete compressor response: {finish_reason or status}')
        if response.message.tool_calls:
            raise ValueError('Compressor returned tool calls instead of text')
        output = ''.join(item.text for item in response.message.content if isinstance(item, TextContent))
        content, closing, _ = output.partition('</step>')
        if not closing and not ('stop' in kwargs and finish_reason == 'stop'):
            raise ValueError('Compressor response is missing </step>')
        opening = re.search(r'''<step\s+id=["'](\d+)["']\s*>''', content[:200])
        if opening:
            if int(opening[1]) != step_index:
                raise ValueError('Compressor returned the wrong step ID')
            content = content[opening.end():]
        elif not self.assistant_prefill:
            raise ValueError('Compressor response is missing the target step wrapper')
        if '<step' in content:
            raise ValueError('Compressor returned multiple or nested steps')
        if not content.strip():
            raise ValueError('Compressor returned no text')
        return content

    def compress_exact_result(self, context: str, *, step_index: int):
        from ..compat.trae_diet import parse_response
        if self.protocol == 'responses-step-wrapper-v1':
            return self._compress_adapted_result(context, step_index=step_index)
        messages = build_compression_messages(context, step_index, self.policy)
        with compression_call(step_index):
            self.last_request_id = context_fields()['request_id']
            answer, finish_reason, usage = self.transport(messages, [])
            reasons = getattr(self.transport, 'last_finish_reasons', [finish_reason])
            emit('diet_compressor_response', step_index=step_index, answer=answer,
                 finish_reasons=reasons, usage=usage)
            def record(raw_usage):
                if self.on_usage is not None:
                    self.on_usage(raw_usage)
                else:
                    self._exact_metrics.record_analysis_usage(raw_usage)
                emit('diet_compressor_usage', step_index=step_index, usage=raw_usage)
            return parse_response(answer, reasons, usage, record)

    def _compress_adapted_result(self, context: str, *, step_index: int):
        from ..diet.prompts import build_adapted_compression_messages
        from ..compat.trae_diet import parse_response
        messages = build_adapted_compression_messages(context, step_index, self.policy)
        with compression_call(step_index):
            answer, reason, usage = self.transport(messages, [])
            self.last_request_id = self.transport.last_request_id
            emit('diet_compressor_response', step_index=step_index, answer=answer,
                 finish_reasons=[reason], usage=usage, compression_protocol=self.protocol)
            def record(raw_usage):
                if self.on_usage is not None:
                    self.on_usage(raw_usage)
                else:
                    self._exact_metrics.record_analysis_usage(raw_usage)
                emit('diet_compressor_usage', step_index=step_index, usage=raw_usage)
            # Responses keeps its own prompt/transport, but uses Trae's literal
            # parser, including acceptance without a closing tag on stop.
            return parse_response(answer, [reason], usage, record)


def build_compressor(llm: Any, *, on_usage: Callable[[dict[str, int]], None] | None = None,
                     assistant_prefill: bool | None = None, transport=None, policy=None) -> LLMCompressor:
    return LLMCompressor(llm, on_usage=on_usage, assistant_prefill=assistant_prefill, transport=transport, policy=policy)
