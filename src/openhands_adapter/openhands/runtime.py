"""OpenHands SDK compatibility and ambient-discovery controls."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any


OPENHANDS_SDK_VERSION = "1.49.5"


def require_pinned_sdk() -> None:
    """Fail closed when the SDK differs from the reviewed integration version."""
    try:
        installed = version("openhands-sdk")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "OpenHands SDK is required; install requirements-openhands.lock"
        ) from exc
    if installed != OPENHANDS_SDK_VERSION:
        raise RuntimeError(
            "Unsupported OpenHands SDK version "
            f"{installed!r}; expected {OPENHANDS_SDK_VERSION!r}. "
            "Install requirements-openhands.lock."
        )


def disable_ambient_discovery() -> None:
    """Disable SDK features that can attach tools or executable project content.

    These are internal SDK hooks, hence the preceding version pin is mandatory.
    This function is called only inside the short-lived agent worker process.
    """
    require_pinned_sdk()
    try:
        import openhands.sdk.agent.base as agent_base
        import openhands.sdk.conversation.impl.local_conversation as local_impl
    except ImportError as exc:
        raise RuntimeError("Pinned OpenHands SDK does not expose discovery controls") from exc

    local_impl.load_available_plugins = lambda **kwargs: {}  # type: ignore[assignment]
    local_impl.load_available_skills = lambda **kwargs: {}  # type: ignore[assignment]
    agent_base.has_vision_profile_available = lambda: False  # type: ignore[assignment]


def sdk_tool_api() -> dict[str, Any]:
    """Load the reviewed tool API lazily, keeping config/test imports SDK-free."""
    require_pinned_sdk()
    try:
        from openhands.sdk import Action, Observation, TextContent, ToolDefinition
        from openhands.sdk.tool import Tool, ToolExecutor, register_tool
    except ImportError as exc:
        raise RuntimeError("Pinned OpenHands SDK does not expose its tool API") from exc
    return {
        "Action": Action,
        "Observation": Observation,
        "TextContent": TextContent,
        "Tool": Tool,
        "ToolDefinition": ToolDefinition,
        "ToolExecutor": ToolExecutor,
        "register_tool": register_tool,
    }
