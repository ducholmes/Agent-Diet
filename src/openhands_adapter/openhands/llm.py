"""Construct OpenHands LLMs for ChatGPT subscriptions or OpenRouter keys.

``auth='subscription'`` is reserved for a ChatGPT Plus/Pro subscription and
delegates OAuth/device-code login to OpenHands. OpenRouter is OpenAI-compatible
API-key authentication, so it always uses ``auth='api-key'``.
"""

from __future__ import annotations

import os
from typing import Any

from ..config import OpenHandsConfig
from .runtime import require_pinned_sdk


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"


def _load_sdk_llm() -> Any:
    require_pinned_sdk()
    try:
        from openhands.sdk import LLM
    except ImportError as exc:
        raise RuntimeError(
            "OpenHands SDK is required to run an agent; install it separately"
        ) from exc
    return LLM


def _api_key(config: OpenHandsConfig) -> str:
    key = os.environ.get(config.api_key_env)
    if not key:
        raise RuntimeError(
            f"Missing API key in environment variable {config.api_key_env!r}"
        )
    return key


def _subscription_llm(LLM: Any, config: OpenHandsConfig, model: str) -> Any:
    if config.base_url:
        raise ValueError(
            "subscription authentication cannot use base_url; use auth='api-key' "
            "with OPENROUTER_API_KEY for OpenRouter"
        )
    auth_method = os.getenv("OPENHANDS_SUBSCRIPTION_AUTH_METHOD", "browser")
    if auth_method not in {"browser", "device_code"}:
        raise ValueError(
            "OPENHANDS_SUBSCRIPTION_AUTH_METHOD must be 'browser' or 'device_code'"
        )
    return LLM.subscription_login(
        vendor=config.subscription_vendor,
        model=model,
        reasoning_effort=config.reasoning_effort,
        auth_method=auth_method,
        force_login=os.getenv("OPENHANDS_SUBSCRIPTION_FORCE_LOGIN") == "1",
    )


def _api_key_llm(LLM: Any, config: OpenHandsConfig, model: str, policy=None) -> Any:
    """Create an OpenAI-compatible LLM, including OpenRouter when configured."""
    base_url = config.base_url
    if config.api_key_env == OPENROUTER_API_KEY_ENV and not base_url:
        base_url = OPENROUTER_BASE_URL
    kwargs: dict[str, Any] = {
        "model": model, "api_key": _api_key(config),
        "reasoning_effort": config.reasoning_effort,
    }
    if base_url:
        kwargs["base_url"] = base_url.rstrip("/")
    if policy is not None:
        kwargs.update(max_output_tokens=8192, top_p=None, top_k=None, seed=None,
                      reasoning_effort=policy.params().get("reasoning_effort"),
                      num_retries=0, drop_params=False, caching_prompt=False, api_mode="chat")
    return LLM(**kwargs)


def build_llm(config: OpenHandsConfig, *, model: str | None = None, policy=None) -> Any:
    """Return an OpenHands LLM using the configured credential source.

    Examples::

        # ChatGPT Plus/Pro (browser OAuth by default)
        --auth subscription --model gpt-5.2-codex

        # OpenRouter (the endpoint is inferred from this key variable)
        --auth api-key --api-key-env OPENROUTER_API_KEY --model openai/gpt-5.2
    """
    config.validate()
    if policy is not None and config.requires_exact_transport:
        policy.validate_transport(subscription=config.auth == "subscription", capabilities=config.trae_capabilities)
    sdk_llm = _load_sdk_llm()
    selected_model = model or config.model
    if config.auth == "subscription":
        return _subscription_llm(sdk_llm, config, selected_model)
    return _api_key_llm(sdk_llm, config, selected_model, policy)


__all__ = ["OPENROUTER_API_KEY_ENV", "OPENROUTER_BASE_URL", "build_llm"]
