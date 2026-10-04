"""Lifecycle for the hardened, repair-only Docker container."""
from __future__ import annotations

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


def start(workspace: Path, *, image: str, runtime: str = "docker") -> RepairContainer:
    root = workspace.resolve()
    name = "agent-diet-oh-" + uuid.uuid4().hex[:16]
    subprocess.run((runtime, "image", "inspect", image), check=True, capture_output=True, text=True, timeout=60)
    command = (
        runtime, "run", "--detach", "--rm", "--pull=never", "--name", name,
        "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,nosuid,nodev",
        "--workdir", str(root), "--mount", f"type=bind,src={root},dst={root}",
        "--entrypoint", "/bin/sh", image, "-c", "while :; do sleep 3600; done",
    )
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    return RepairContainer(name, root, image, runtime)


def remove(container: RepairContainer) -> None:
    subprocess.run((container.runtime, "rm", "-f", container.name), capture_output=True, text=True, timeout=30)
