"""Stable, atomic artifact writing outside disposable workspaces."""
from __future__ import annotations
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
def _json(value: Any) -> Any:
    if isinstance(value, Path): return str(value)
    if is_dataclass(value): return _json(asdict(value))
    if isinstance(value, dict): return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)): return [_json(v) for v in value]
    return value
def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(_json(value), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"); temporary.replace(path)
def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value, encoding="utf-8")
