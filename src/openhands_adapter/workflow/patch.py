"""Git-baseline, patch capture, and safe application helpers."""
from __future__ import annotations
import os
import subprocess
from pathlib import Path

PROTECTED_PATHS = (".git", ".agent-diet", ".agent-diet.repair-config.json", "failure.log")
class PatchError(RuntimeError): pass

def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("git", "-C", str(path), *args), capture_output=True, text=True, check=check)

def create_baseline(workspace: Path, failure_log: Path | None = None) -> None:
    if not (workspace / ".git").exists(): _git(workspace, "init")
    _git(workspace, "config", "user.email", "agent-diet@local"); _git(workspace, "config", "user.name", "Agent Diet")
    if failure_log and failure_log.is_file(): (workspace / ".agent-diet.failure.log").write_bytes(failure_log.read_bytes())
    _git(workspace, "add", "-A"); _git(workspace, "commit", "--allow-empty", "-m", "agent-diet baseline")
    if _git(workspace, "status", "--porcelain").stdout.strip(): raise PatchError("Repair workspace is dirty after baseline")

def capture_patch(workspace: Path) -> bytes:
    result = _git(workspace, "diff", "--binary", "--no-ext-diff", "HEAD"); untracked = _git(workspace, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    if untracked: _git(workspace, "add", "-N", "--", *untracked); result = _git(workspace, "diff", "--binary", "--no-ext-diff", "HEAD")
    return result.stdout.encode()

def changed_paths(patch: bytes) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        line[6:] for line in patch.decode(errors="replace").splitlines()
        if line.startswith(("+++ b/", "--- a/"))
    ))

def assert_safe_patch(patch: bytes) -> None:
    for path in changed_paths(patch):
        if path == "/dev/null" or path.startswith("/") or ".." in Path(path).parts or any(path == item or path.startswith(item + "/") for item in PROTECTED_PATHS): raise PatchError(f"Protected or unsafe patch path: {path}")

def apply_patch(workspace: Path, patch: bytes) -> None:
    assert_safe_patch(patch)
    # Treat paths relative to this checkout even when it has no .git and sits
    # inside the adapter repository. Otherwise Git can silently skip the patch
    # as outside the current repository prefix while returning success.
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(workspace.resolve().parent)}
    for args in (("apply", "--check", "--binary", "-"), ("apply", "--binary", "-")):
        result = subprocess.run(("git", "-C", str(workspace), *args), input=patch, capture_output=True, env=env)
        if result.returncode: raise PatchError(result.stderr.decode(errors="replace").strip() or "git apply failed")
