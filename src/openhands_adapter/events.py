"""Small JSONL event sink shared by the host workflow and OpenHands worker."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any


_event_path: Path | None = None
_event_lock = Lock()


def configure(path: Path | None, *, reset: bool = False) -> None:
    """Select the JSONL file used by :func:`emit` in this process."""
    global _event_path
    _event_path = path.resolve() if path else None
    if _event_path:
        _event_path.parent.mkdir(parents=True, exist_ok=True)
        if reset:
            _event_path.write_text("", encoding="utf-8")


def emit(event_type: str, **fields: Any) -> None:
    """Append one self-contained, JSON-serializable event."""
    if _event_path is None:
        return
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        **fields,
    }
    payload = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode(
        "utf-8"
    )
    # A single append write keeps host and worker records intact when both
    # processes are writing the same events.jsonl during the repair phase.
    with _event_lock:
        descriptor = os.open(
            _event_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
        finally:
            os.close(descriptor)


def read_events(path: Path) -> list[dict[str, Any]]:
    """Read valid JSONL records, ignoring a partial last line after a crash."""
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def latest(path: Path, event_type: str) -> dict[str, Any] | None:
    """Return the latest event with ``event_type``."""
    for event in reversed(read_events(path)):
        if event.get("type") == event_type:
            return event
    return None


__all__ = ["configure", "emit", "latest", "read_events"]
