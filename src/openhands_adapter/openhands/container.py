"""Lifecycle for the hardened, repair-only Docker container."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RepairContainer:
    name: str
    workspace: Path
    image: str
    runtime: str = "docker"
    exact_trae: bool = False
    image_id: str | None = None


def start(workspace: Path, *, image: str, runtime: str = "docker", exact_trae: bool = False) -> RepairContainer:
    root = workspace.resolve()
    name = "agent-diet-oh-" + uuid.uuid4().hex[:16]
    inspected = subprocess.run((runtime, "image", "inspect", image), check=True, capture_output=True, text=True, timeout=60)
    try:
        image_id = json.loads(inspected.stdout)[0]['Id']
    except (ValueError, TypeError, KeyError):
        image_id = None
    command = (
        runtime, "run", "--detach", "--rm", "--pull=never", "--name", name,
        "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,nosuid,nodev",
        "--workdir", str(root), "--mount", f"type=bind,src={root},dst={root}",
        "--entrypoint", "/bin/sh", image, "-c", "while :; do sleep 3600; done",
    )
    if exact_trae:
        command = (
            runtime, "run", "--detach", "--rm", "--pull=never", "--name", name,
            "--security-opt=no-new-privileges", "--workdir", str(root),
            "--mount", f"type=bind,src={root},dst={root}",
            "--entrypoint", "/bin/sh", image, "-c", "while :; do sleep 3600; done",
        )
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    return RepairContainer(name, root, image, runtime, exact_trae, image_id)


def remove(container: RepairContainer) -> None:
    try:
        if container.exact_trae:
            # Original tools run as root. Return ownership of the disposable
            # bind mount so the host can retain or clean new build directories.
            subprocess.run((container.runtime, "exec", container.name, "chown", "-hR",
                            f"{os.getuid()}:{os.getgid()}", str(container.workspace)),
                           check=True, capture_output=True, text=True, timeout=60)
    finally:
        subprocess.run((container.runtime, "rm", "-f", container.name), capture_output=True, text=True, timeout=30)
