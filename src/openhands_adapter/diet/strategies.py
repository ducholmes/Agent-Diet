"""Reduction strategies.  They do not know about an agent SDK."""
from __future__ import annotations

import random
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from .core import _token_encoding

Compressor = Callable[[str, str], str]

def delete(_: str, __: str = "") -> str: return ""

def random_drop(text: str, _: str = "", *, ratio: float = .25, seed: int | None = None) -> str:
    """Drop gpt-4o tokens that individually decode as UTF-8, as in Trae."""
    if not 0 <= ratio <= 1:
        raise ValueError("ratio must be in [0, 1]")
    encoding = _token_encoding()
    tokens = encoding.encode(text, disallowed_special=())
    deletable_indices = []
    for index, token in enumerate(tokens):
        try:
            encoding.decode_single_token_bytes(token).decode("utf-8")
        except UnicodeDecodeError:
            continue
        deletable_indices.append(index)
    rng = random if seed is None else random.Random(seed)
    deleted_indices = set(rng.sample(
        deletable_indices, min(int(len(tokens) * (1 - ratio)), len(deletable_indices)),
    ))
    return encoding.decode([token for index, token in enumerate(tokens) if index not in deleted_indices])


@lru_cache(maxsize=1)
def _lingua_compressor() -> Any:
    from llmlingua import PromptCompressor

    return PromptCompressor(
        model_name="microsoft/llmlingua-2-xlm-roberta-large-meetingbank",
        use_llmlingua2=True,
        device_map="cpu",
    )

def lingua(text: str, _: str = "", *, ratio: float = .25) -> str:
    """Use cached LLMLingua2; failures propagate instead of changing baseline."""
    return _lingua_compressor().compress_prompt(
        text, rate=ratio, force_tokens=["\n", "?"],
    )["compressed_prompt"]

def ours(text: str, context: str, compressor: Compressor) -> str:
    return compressor(text, context)
