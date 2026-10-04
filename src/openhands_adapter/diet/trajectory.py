"""Stable, SDK-independent conversion of conversation events into steps."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


def _value(event: Any, name: str, default: Any = None) -> Any:
    return event.get(name, default) if isinstance(event, Mapping) else getattr(event, name, default)


def event_id(event: Any, fallback: int) -> str:
    return str(_value(event, "id", _value(event, "event_id", fallback)))


def event_tool_call_id(event: Any) -> str | None:
    """Return the tool-call ID shared by an action and its observation."""
    value = _value(event, "tool_call_id")
    if value is None:
        value = _value(event, "call_id")
    return str(value) if value is not None else None


def event_text(event: Any) -> str:
    # Read the same content the SDK sends to either Chat or Responses APIs.
    convert = getattr(event, "to_llm_message", None)
    if callable(convert):
        return _content_text(convert().content)
    message = _value(event, "llm_message")
    if message is not None:
        content = _value(message, "content", [])
        if isinstance(content, str):
            return content
        return "".join(str(_value(item, "text", "")) for item in content)
    summary = _value(event, "summary")
    if isinstance(summary, str) and "condensationsummary" in type(event).__name__.lower():
        return summary
    for name in ("content", "text", "message", "action", "observation", "output"):
        value = _value(event, name)
        if isinstance(value, str): return value
        if value is not None: return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)


def event_kind(event: Any) -> str:
    """Return a stable kind for mappings and OpenHands SDK event objects."""
    explicit = _value(event, "type", _value(event, "kind"))
    if explicit:
        return str(explicit).lower()
    return type(event).__name__.lower()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(str(_value(item, "text", "")) for item in (content or []))


@dataclass(frozen=True, slots=True)
class LogicalStep:
    index: int
    event_ids: tuple[str, ...]
    text: str
    complete: bool = True

    def serialize(self) -> str:
        if self.index == -1:
            return self.text
        return f'<step id="{self.index}">\n{self.text}\n</step>'


def logical_steps(events: Iterable[Any]) -> tuple[LogicalStep, ...]:
    """One model response and all its tool results form one Trae-style step.

    Initial user content is context -1, system events are outside the trajectory.
    Unowned events stay in the SDK view without becoming counted steps.
    Missing response IDs use legacy call pairing;
    actual SDK actions carry response IDs on both supported provider paths.
    """
    groups: list[dict[str, Any]] = []
    by_response: dict[str, dict[str, Any]] = {}
    by_call: dict[str, tuple[dict[str, Any], Any, str]] = {}
    by_action: dict[str, tuple[dict[str, Any], Any, str]] = {}
    unkeyed: list[tuple[dict[str, Any], Any, str]] = []
    initial_ids: list[str] = []
    initial_text: list[str] = []

    def new_group() -> dict[str, Any]:
        group: dict[str, Any] = {"ids": [], "head": [], "calls": [], "tail": [],
                                 "pending": set()}
        groups.append(group)
        return group

    for position, event in enumerate(events):
        kind = event_kind(event)
        eid = event_id(event, position)
        message = _value(event, "llm_message")
        role = _value(message, "role", _value(event, "role"))
        source = _value(event, "source")
        if "systemprompt" in kind or kind == "system_prompt" or role == "system":
            continue
        is_action = any(word in kind for word in ("action", "toolcall", "tool_call"))
        is_result = (any(word in kind for word in ("observation", "toolresult", "tool_result"))
                     or kind in {"agenterrorevent", "agent_error"})
        call_id = event_tool_call_id(event)
        response_id = _value(event, "llm_response_id")

        if is_action:
            group = by_response.get(str(response_id)) if response_id is not None else None
            if group is None:
                group = new_group()
                if response_id is not None:
                    by_response[str(response_id)] = group
            group["ids"].append(eid)
            thought = _content_text(_value(event, "thought", []))
            if thought.strip():
                group["head"].append(f"<think>{thought}</think>")
            call = _value(event, "tool_call")
            name = _value(call, "name", _value(event, "tool_name", ""))
            arguments = _value(call, "arguments")
            if arguments is None:
                action = _value(event, "action", "")
                dump = getattr(action, "model_dump", None)
                arguments = (action if isinstance(action, str) else
                             json.dumps(dump() if callable(dump) else action,
                                        ensure_ascii=False, default=str))
            group["calls"].append(f'<call tool="{name}">{arguments.strip()}</call>')
            group["pending"].add(eid)
            by_action[eid] = (group, event, eid)
            if call_id is not None:
                by_call[call_id] = (group, event, eid)
            else:
                unkeyed.append((group, event, eid))
        elif is_result:
            match = by_call.pop(call_id, None) if call_id is not None else None
            if match is None:
                action_id = _value(event, "action_id")
                match = by_action.get(str(action_id)) if action_id is not None else None
            if match is None and call_id is None and unkeyed:
                match = unkeyed.pop(0)
            if match is None:
                continue
            group, action, action_eid = match
            group["ids"].append(eid)
            group["pending"].discard(action_eid)
            name = _value(action, "tool_name", _value(_value(action, "tool_call"), "name", ""))
            if (str(name).lower() in {"think", "thinktool"}
                    and kind in {"observation", "observationevent", "toolresult", "tool_result"}):
                arguments = _value(_value(action, "tool_call"), "arguments")
                try:
                    parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                except (ValueError, TypeError):
                    parsed = None
                thought = _value(parsed, "thought", _value(_value(action, "action"), "thought", None))
                if thought is None:
                    content = event_text(event)
                    if content.strip():
                        group["tail"].append(f"<result>{content}</result>")
                    continue
                content = f"\n{str(thought).strip()}\n"
                group["tail"].append(f"<think>{content}</think>")
            else:
                content = event_text(event)
                if content.strip():
                    group["tail"].append(f"<result>{content}</result>")
        elif role == "user" or source == "user":
            content = event_text(event)
            if not groups:
                initial_ids.append(eid)
                initial_text.append(content)
            else:
                groups[-1]["ids"].append(eid)
                if content.strip():
                    groups[-1]["tail"].append(f"<user>{content}</user>")
        else:
            is_summary = "condensationsummary" in kind
            is_message = (role == "assistant" or kind in {"message", "messageevent"}
                          or (isinstance(event, Mapping) and kind == "dict" and "content" in event))
            if not is_message and not is_summary:
                continue
            group = by_response.get(str(response_id)) if response_id is not None else None
            if group is None:
                group = new_group()
                if response_id is not None:
                    by_response[str(response_id)] = group
            group["ids"].append(eid)
            content = event_text(event)
            if is_message:
                if content.strip():
                    group["head"].append(f"<think>{content}</think>")
            else:
                group["head"].append(content)

    steps = ([LogicalStep(-1, tuple(initial_ids), "\n".join(initial_text))]
             if initial_ids else [])
    steps.extend(LogicalStep(index, tuple(group["ids"]),
                             "\n".join(group["head"] + group["calls"] + group["tail"]),
                             not group["pending"])
                 for index, group in enumerate(groups))
    return tuple(steps)
