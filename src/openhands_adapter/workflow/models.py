"""Data contracts for deterministic workflow stages."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from ..input_loader import CaseSpec, CommandSpec

@dataclass(frozen=True, slots=True)
class RunLimits:
    command_timeout_seconds: int; validation_timeout_seconds: int; output_limit_bytes: int

@dataclass(slots=True)
class CommandResult:
    argv: tuple[str, ...]; cwd: str; exit_code: int | None; elapsed_seconds: float
    stdout: str; stderr: str; timed_out: bool = False
    label: str = ""; command: tuple[str, ...] = (); log_path: str | None = None
    passed: bool | None = None; failure_reason: str | None = None
    @property
    def succeeded(self) -> bool: return self.exit_code == 0 and not self.timed_out

@dataclass(slots=True)
class BaselineResult:
    clean: bool; checks: dict[str, str] = field(default_factory=dict); reason: str | None = None
    test_evidence: dict[str, Any] = field(default_factory=dict)

@dataclass(slots=True)
class ValidationResult:
    passed: bool; phase: str; commands: list[CommandResult] = field(default_factory=list); reason: str | None = None
    status: str = "invalid"; target_failures: tuple[str, ...] = (); regression_failures: tuple[str, ...] = ()
    failed_test_ids: tuple[str, ...] = (); fixed_test_ids: tuple[str, ...] = (); regression_test_ids: tuple[str, ...] = ()
    target_valid: bool = False; regression_valid: bool = False; environment_ready: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

@dataclass(slots=True)
class RunResult:
    case_id: str; baseline: str; agent: str; patch_generated: bool; patch_applied: bool
    validation: str; resolved: bool; reason: str | None = None
    run_id: str | None = None
    patch_origin: str | None = None
    audit_status: str = "not_verified"
    elapsed_seconds: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)
    token_usage: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    baseline_summary: dict[str, Any] = field(default_factory=dict)
    agent_summary: dict[str, Any] = field(default_factory=dict)
    validation_summary: dict[str, Any] = field(default_factory=dict)
    diet_metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]: return asdict(self)

__all__ = ["CaseSpec", "CommandSpec", "RunLimits", "CommandResult", "BaselineResult", "ValidationResult", "RunResult"]
