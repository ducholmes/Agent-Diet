"""Small, stderr-based progress logger for interactive runs."""
from __future__ import annotations

import shlex
import sys
from typing import Any

from .events import emit
from .compat.audit import sanitize


def log(message: str) -> None:
    """Write one immediately visible progress line without touching stdout."""
    print(f"[agent-diet] {sanitize(message)}", file=sys.stderr, flush=True)


def stage(name: str, status: str, **fields: Any) -> None:
    """Write a structured stage event suitable for a human terminal log."""
    emit("stage", step=name, status=status, **fields)
    values = [f"step={name}", f"status={status}"]
    for key, value in fields.items():
        if value is not None:
            values.append(f"{key}={shlex.quote(str(value))}")
    log(" ".join(values))


__all__ = ["log", "stage"]
