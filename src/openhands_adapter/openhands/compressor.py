"""Trae compression prompt and response protocol on the OpenHands LLM API."""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..diet.prompts import SYSTEM_PROMPT
from ..events import emit
from ..token_tracking import compression_call


def _value(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class LLMCompressor:
    def __init__(self, llm: Any, *, on_usage: Callable[[dict[str, int]], None] | None = None,
                 assistant_prefill: bool = False) -> None:
        self.llm, self.on_usage, self.assistant_prefill = llm, on_usage, assistant_prefill

    def __call__(self, text: str, context: str) -> str:
        for match in re.finditer(r'<step id="(\d+)">\n', context):
            if context.startswith(text + '\n</step>', match.end()):
                return self.compress_step(text, context, step_index=int(match[1]))
        return self.compress_step(text, f'<step id="0">\n{text}\n</step>\n{context}', step_index=0)

    def compress_step(self, text: str, context: str, *, step_index: int) -> str:
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


def build_compressor(llm: Any, *, on_usage: Callable[[dict[str, int]], None] | None = None,
                     assistant_prefill: bool = False) -> LLMCompressor:
    return LLMCompressor(llm, on_usage=on_usage, assistant_prefill=assistant_prefill)
