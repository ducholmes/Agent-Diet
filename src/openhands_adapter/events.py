"""Process-safe JSONL audit with strict integrity and isolated sink failures."""
from __future__ import annotations
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4
from .compat.audit import SCHEMA_VERSION, context_fields, retention_copy

_event_path: Path | None = None
_event_lock = Lock()
_identity: dict = {}
_raw = True
_seq = 0
_errors: list[dict] = []


def configure(path: Path | None, *, reset=False, run_id=None, case_id=None, raw=True, producer=None):
    global _event_path, _identity, _raw, _seq, _errors
    _event_path = path.resolve() if path else None
    _raw, _seq, _errors = raw, 0, []
    _identity = {'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'case_id': case_id,
                 'producer': producer or str(uuid4())}
    if _event_path:
        _event_path.parent.mkdir(parents=True, exist_ok=True)
        if reset:
            _event_path.write_bytes(b'')


def identity():
    return dict(_identity)


def audit_errors():
    return list(_errors)


def record_audit_error(phase, error):
    record = {'phase': phase, 'error_type': type(error).__name__}
    _errors.append(record)
    # Persist a separate marker if the primary sink failed; never retry models.
    if _event_path is not None:
        try:
            path = _event_path.with_name(f'audit-error-{_identity.get("producer")}.json')
            path.write_text(json.dumps(_errors) + '\n')
        except Exception:
            pass


def _write_bytes(descriptor, data):
    return os.write(descriptor, data)


def emit(event_type: str, **fields: Any):
    global _seq
    if _event_path is None:
        return
    try:
        with _event_lock:
            next_seq = _seq + 1
            record = {'timestamp': datetime.now(timezone.utc).isoformat(),
                      **_identity, **context_fields(), 'producer_seq': next_seq,
                      'record_id': f'{_identity["producer"]}:{next_seq}', 'type': event_type,
                      **retention_copy(fields, _raw)}
            payload = (json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
            descriptor = os.open(_event_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                offset = 0
                while offset < len(payload):
                    written = _write_bytes(descriptor, payload[offset:])
                    if written <= 0:
                        raise OSError('zero-byte audit write')
                    offset += written
                _seq = next_seq
            finally:
                os.close(descriptor)
    except Exception as error:
        record_audit_error('event_write', error)


def read_events_strict(path: Path):
    if not path.is_file():
        return [], ['missing_events']
    records, issues, ids, sequences = [], [], {}, {}
    lines = path.read_bytes().splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.endswith(b'\n'):
            issues.append(f'truncated_tail:{index+1}')
            break
        try:
            event = json.loads(line.decode('utf-8'))
            if not isinstance(event, dict):
                raise ValueError('record must be an object')
        except (ValueError, UnicodeDecodeError):
            issues.append(f'corrupt_record:{index+1}')
            continue
        if type(event.get('schema_version')) is not int or event['schema_version'] != SCHEMA_VERSION:
            issues.append(f'unsupported_schema:{index+1}')
        rid = event.get('record_id')
        if not isinstance(rid, str) or not rid:
            issues.append(f'missing_record_id:{index+1}')
        elif rid in ids:
            if ids[rid] != event:
                issues.append(f'duplicate_conflict:{rid}')
            else:
                issues.append(f'duplicate_record:{rid}')
            continue
        if isinstance(rid, str) and rid:
            ids[rid] = event
        producer, seq = event.get('producer'), event.get('producer_seq')
        if not isinstance(producer, str) or not producer:
            issues.append(f'invalid_producer:{index+1}')
            records.append(event)
            continue
        if type(seq) is int and rid != f'{producer}:{seq}':
            issues.append(f'record_id_mismatch:{index+1}')
        if type(seq) is not int or seq != sequences.get(producer, 0)+1:
            issues.append(f'sequence_gap:{index+1}')
        if type(seq) is int:
            sequences[producer] = seq
        records.append(event)
    return records, issues


def read_events(path: Path):
    """Recovery reader accepts a labelled partial tail, rejects middle corruption."""
    if not path.is_file():
        return []
    records, issues = read_events_strict(path)
    corrupt = [issue for issue in issues if issue.startswith('corrupt_record:')]
    if corrupt:
        raise ValueError('event integrity failure: ' + ', '.join(corrupt))
    return records


def recover_events(path: Path):
    """Telemetry recovery never controls generation; integrity stays explicit."""
    try:
        records, issues = read_events_strict(path)
        if issues:
            record_audit_error('event_integrity', ValueError(','.join(issues)))
        return records
    except Exception as error:
        record_audit_error('event_read', error)
        return []


def latest(path: Path, event_type: str):
    return next((e for e in reversed(read_events(path)) if e.get('type') == event_type), None)
