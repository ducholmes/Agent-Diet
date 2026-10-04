"""Small fail-closed command guard; container mounts remain the real boundary."""
from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GuardDecision:
    allowed: bool
    reason: str = ""


class CommandGuard:
    """Reject known benchmark access and paths outside the repair workspace."""

    _LOOKUP = re.compile(r"\b(?:gold(?:en)?[_ -]?patch|reference[_ -]?solution|benchmark[_ -]?(?:answer|lookup)|swe-bench.*(?:solution|patch))\b", re.I)

    def __init__(self, workspace: Path, *, sensitive_roots: tuple[Path, ...] = (), audit_log: Path | None = None):
        self.workspace = workspace.resolve()
        self.sensitive_roots = tuple(path.resolve() for path in sensitive_roots)
        self.audit_log = audit_log

    def check(self, command: str) -> GuardDecision:
        if self._LOOKUP.search(command):
            return self._audit(GuardDecision(False, "benchmark_lookup_attempt"), command)
        try:
            tokens = shlex.split(command)
        except ValueError:
            return self._audit(GuardDecision(False, "invalid_shell_quoting"), command)
        for token in tokens:
            if token == ".." or token.startswith("../") or "/../" in token:
                return self._audit(GuardDecision(False, "workspace_parent_escape"), command)
            if not token.startswith("/"):
                continue
            path = Path(token)
            if str(path).startswith(("/proc", "/sys", "/dev", "/var/run/docker.sock")):
                return self._audit(GuardDecision(False, "sensitive_system_path"), command)
            if any(self._within(path, root) for root in self.sensitive_roots):
                return self._audit(GuardDecision(False, "sensitive_root_access"), command)
            if not self._within(path, self.workspace) and not str(path).startswith(("/bin/", "/usr/bin/", "/usr/local/bin/", "/tmp/")):
                return self._audit(GuardDecision(False, "workspace_escape"), command)
        return GuardDecision(True)

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root)
            return True
        except ValueError:
            return False

    def _audit(self, decision: GuardDecision, command: str) -> GuardDecision:
        if self.audit_log:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({**asdict(decision), "command": command, "timestamp": time.time()}) + "\n")
        return decision
