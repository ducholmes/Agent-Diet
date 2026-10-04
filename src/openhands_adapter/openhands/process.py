"""Outer worker process with ContextSniper-style timeout and container cleanup."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Thread
from typing import TextIO

from ..config import AgentDietConfig, OpenHandsConfig, WorkflowConfig
from ..progress import stage
from .container import remove, start


WORKER_HEARTBEAT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class WorkerResult:
    response: str
    returncode: int
    elapsed_seconds: float
    timed_out: bool


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _serializable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def _capture_worker_stream(stream: TextIO, path: Path, *, forward_progress: bool) -> None:
    """Persist worker output and forward our structured progress lines to stderr."""
    with path.open("w", encoding="utf-8") as handle:
        for line in iter(stream.readline, ""):
            handle.write(line)
            handle.flush()
            if forward_progress and line.lstrip().startswith("[agent-diet]"):
                print(line.rstrip("\n"), file=sys.stderr, flush=True)
    stream.close()


def run_worker(workspace: Path, prompt: str, output_dir: Path, *, execution_plan: dict, image: str, runtime: str, openhands: OpenHandsConfig, diet: AgentDietConfig, workflow: WorkflowConfig) -> WorkerResult:
    """Create repair container, supervise worker process, and always remove it."""
    if output_dir.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("Worker configuration/output must be outside the repair workspace")
    logs = output_dir / "logs"; logs.mkdir(parents=True, exist_ok=True)
    response_path, stdout_path, stderr_path = output_dir / "response.txt", logs / "worker.stdout.jsonl", logs / "worker.stderr.log"
    stage(
        "repair",
        "starting",
        agent="OpenHands",
        model=openhands.model,
        auth=openhands.auth,
        compressor_mode=diet.mode,
        compressor_model=openhands.model if diet.compressor_model == "inherit" else diet.compressor_model,
    )
    container = start(workspace, image=image, runtime=runtime)
    temporary_root = output_dir / ".tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    config_dir = tempfile.TemporaryDirectory(prefix="agent-diet-worker-", dir=temporary_root)
    config_path = Path(config_dir.name) / "worker-config.json"
    config = {
        "execution_plan": execution_plan,
        "workspace": str(workspace.resolve()), "output_dir": str(output_dir.resolve()), "response_path": str(response_path.resolve()),
        "container_name": container.name, "image": image, "runtime": runtime,
        "openhands": _serializable(asdict(openhands)), "agentdiet": _serializable(asdict(diet)), "workflow": _serializable(asdict(workflow)),
    }
    started, timed_out = time.monotonic(), False
    process = None
    try:
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        process = subprocess.Popen(
            (sys.executable, "-m", "openhands_adapter.openhands.worker", str(config_path)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout_thread = Thread(target=_capture_worker_stream, args=(process.stdout, stdout_path), kwargs={"forward_progress": False}, daemon=True)
        stderr_thread = Thread(target=_capture_worker_stream, args=(process.stderr, stderr_path), kwargs={"forward_progress": True}, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            assert process.stdin is not None
            process.stdin.write(prompt); process.stdin.close()
            deadline = started + workflow.agent_timeout_seconds
            next_heartbeat = started + WORKER_HEARTBEAT_SECONDS
            print(
                f"[agent-diet] worker started (pid={process.pid}); "
                f"heartbeat={WORKER_HEARTBEAT_SECONDS}s",
                file=sys.stderr,
                flush=True,
            )
            while process.poll() is None and time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_heartbeat:
                    elapsed = now - started
                    try:
                        log_bytes = stdout_path.stat().st_size + stderr_path.stat().st_size
                    except OSError:
                        log_bytes = 0
                    print(
                        f"[agent-diet] worker still running ({elapsed:.0f}s, logs={log_bytes} bytes)",
                        file=sys.stderr,
                        flush=True,
                    )
                    next_heartbeat = now + WORKER_HEARTBEAT_SECONDS
                time.sleep(.25)
            if process.poll() is None:
                timed_out = True; _stop(process)
            returncode = process.wait(timeout=5) if process.poll() is None else process.returncode
            elapsed = time.monotonic() - started
            stage("repair", "timed_out" if timed_out else "finished", agent="OpenHands", model=openhands.model, returncode=returncode, elapsed=f"{elapsed:.1f}s")
        finally:
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
        response = response_path.read_text(encoding="utf-8") if response_path.is_file() else ""
        return WorkerResult(response, int(returncode or 0), time.monotonic() - started, timed_out)
    finally:
        try:
            if process is not None:
                _stop(process)
        finally:
            try:
                remove(container)
            finally:
                config_dir.cleanup()
                try:
                    temporary_root.rmdir()
                except OSError:
                    pass
                # Empty stream/audit files add no diagnostic information.
                for path in (stdout_path, stderr_path, logs / "command-guard.jsonl"):
                    if path.is_file() and not path.is_symlink() and path.stat().st_size == 0:
                        path.unlink()
                try:
                    logs.rmdir()
                except OSError:
                    pass
