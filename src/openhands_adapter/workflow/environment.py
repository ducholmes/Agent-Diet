"""Fail-closed Docker validation environment, matching ContextSniper policy."""
from __future__ import annotations

import shutil
import os
import subprocess
from pathlib import Path

from ..input_loader import EnvironmentSpec


class EnvironmentError(RuntimeError):
    """The declared validation runtime cannot be used safely."""


def ensure_available(environment: EnvironmentSpec, *, timeout_seconds: int) -> str:
    """Require the configured runtime and already-built image; never pull/build."""
    if environment.mode != "image":
        raise EnvironmentError(f"unsupported validation environment: {environment.mode}")
    if not shutil.which(environment.runtime):
        raise EnvironmentError(f"runtime unavailable: {environment.runtime}")
    probe_timeout = min(timeout_seconds, 60)
    for command, label in (
        ((environment.runtime, "info"), "runtime unavailable"),
        ((environment.runtime, "image", "inspect", "--format", "{{.Id}}", environment.image), "image unavailable"),
    ):
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=probe_timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise EnvironmentError(f"{label}: probe timed out") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            raise EnvironmentError(f"{label}: {environment.image if 'image' in label else environment.runtime}: {detail}")

    return completed.stdout.strip()


def docker_command(environment: EnvironmentSpec, workspace: Path, cwd: str, argv: tuple[str, ...]) -> tuple[tuple[str, ...], Path]:
    """Return a Docker command that exposes only the validation checkout."""
    root = workspace.resolve()
    relative_cwd = (root / cwd).resolve().relative_to(root).as_posix()
    container_cwd = "/testbed" if relative_cwd == "." else f"/testbed/{relative_cwd}"
    return (
        (environment.runtime, "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{root}:/testbed:rw", "-w", container_cwd, environment.image, *argv),
        root,
    )
