"""Repair instructions adapted from Trae, alongside the SDK's tool guidance."""
from __future__ import annotations

from pathlib import Path


REPAIR_SYSTEM_PROMPT = """
Follow this bug repair workflow methodically. Only production code may be
changed in the submitted patch; this restriction also applies to verification.

1. Understand the problem: read the task description and failure log, identify
   the observed failure and expected behavior.
2. Explore and locate: inspect the relevant source code, existing tests, and
   examples to understand the affected components.
3. Reproduce before editing production code: inspect list_configured_commands,
   then run the configured setup/build and target_test using phase and index
   (zero-based). The executor supplies the configured argv and cwd.
   Inspect the output to confirm the reported bug. Report blockers explicitly.
4. Diagnose: trace the relevant execution flow and determine the root cause
   before implementing a fix.
5. Implement: make the smallest clean, targeted production-code change that
   addresses the root cause. Do not edit or add repository tests, test fixtures,
   evaluator configuration, or Agent-Diet artifacts (including the failure log).
   Do not select extra tests or create reproduction/demo scripts.
6. Verify: rebuild as needed and run the configured target_test and
   regression_test commands. Inspect the final diff with inspect_workspace_diff.
7. Summarize and finish: use FinishTool to report the root cause, the fix,
   the reproduction and test commands with their observed results, and any
   unresolved failures or blockers. Verify before reporting success.
""".strip()


DEFAULT_TASK_PROMPT = "Diagnose and fix the reported failure."


def build_generic_user_prompt(
    project_path: Path, *, problem_statement: str | None = None,
    instructions: str = DEFAULT_TASK_PROMPT,
) -> str:
    """Include mandatory case context even when CLI instructions are customized."""
    parts = [f"[Project root path in the repair container]:\n{project_path}"]
    parts.append(
        "[Buggy source code]:\n"
        "The project root above contains a writable copy of the selected case's "
        "buggy source code. Use the workspace tools to list, search, and read "
        "the source files there, then edit the relevant production code."
    )
    if problem_statement:
        parts.append(f"[Problem statement]:\n{problem_statement}")
    parts.append(f"[Task instructions]:\n{instructions}")
    parts.append(
        "[Repair constraints]:\n"
        "Read .agent-diet.failure.log in the project root before diagnosing the bug. "
        "Edit only the production code needed to fix it. Do not modify or add "
        "repository tests, test fixtures, evaluator configuration, or Agent-Diet "
        "artifacts, including .agent-diet.failure.log. Only use run_configured_command "
        "for setup, build, and test execution. "
        "Do not look for, copy, or use external/reference solutions, git history, "
         "benchmark metadata, generated validation artifacts, or network resources."
    )
    return "\n\n".join(parts)


from ..compat.trae_contract import SYS_PROMPT, INIT_USER_PROMPT

TRAE_REPAIR_SYSTEM_PROMPT = SYS_PROMPT


def build_user_prompt(project_path: Path, *, problem_statement: str | None = None,
                      failure_log: str | None = None, instructions: str | None = None,
                      reference_profile: str = "trae_verified") -> str:
    if reference_profile == "generic":
        return build_generic_user_prompt(project_path, problem_statement=problem_statement,
                                         instructions=instructions or DEFAULT_TASK_PROMPT)
    if instructions is not None:
        raise ValueError("prompt overrides are outside the Trae contract; use generic mode")
    issue = problem_statement if problem_statement is not None else failure_log
    if not isinstance(issue, str) or not issue.strip():
        raise ValueError("Trae repair requires nonempty problem_statement or prepared failure log")
    # Preserve issue bytes decoded as UTF-8, including leading/trailing whitespace.
    return INIT_USER_PROMPT.format(project_path=project_path, issue=issue)
