"""Bounded host/Docker command execution with auditable per-command logs."""
from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from ..input_loader import CommandSpec, EnvironmentSpec
from ..progress import stage
from .environment import docker_command
from .models import CommandResult


def _limit(value: str, maximum: int | None) -> str:
    if maximum is None:
        return value
    return value[:maximum] + ("\n[truncated]" if len(value) > maximum else "")


def _write_log(path: Path, result: CommandResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"COMMAND: {shlex.join(result.command or result.argv)}\nCWD: {result.cwd}\n"
        f"RETURN_CODE: {result.exit_code}\nTIMED_OUT: {result.timed_out}\n"
        f"ELAPSED_SECONDS: {result.elapsed_seconds:.3f}\n\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n",
        encoding="utf-8",
    )


def run_command(
    spec: CommandSpec,
    workspace: Path,
    *,
    timeout_seconds: int,
    output_limit_bytes: int | None = 1_000_000,
    environment: EnvironmentSpec | None = None,
    label: str = "command",
    log_dir: Path | None = None,
) -> CommandResult:
    root = workspace.resolve()
    cwd = (root / spec.cwd).resolve()
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Command cwd escapes workspace: {spec.cwd}") from exc
    command, host_cwd = (
        docker_command(environment, root, spec.cwd, spec.argv)
        if environment else (spec.argv, cwd)
    )
    stage("validation", "command_started", phase=label, command=shlex.join(command), cwd=str(host_cwd))
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=host_cwd, capture_output=True, text=True, timeout=timeout_seconds, check=False)
        stdout, stderr, code, timed_out = completed.stdout, completed.stderr, completed.returncode, False
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        code, timed_out = None, True
    result = CommandResult(spec.argv, spec.cwd, code, time.monotonic() - started, _limit(stdout, output_limit_bytes), _limit(stderr, output_limit_bytes), timed_out, label, tuple(command))
    if log_dir:
        log_path = log_dir / f"{label.replace('/', '_')}.log"
        _write_log(log_path, result)
        result.log_path = str(log_path)
    stage(
        "validation",
        "command_finished",
        phase=label,
        returncode=result.exit_code,
        timed_out=result.timed_out,
        elapsed=f"{result.elapsed_seconds:.1f}s",
    )
    return result
