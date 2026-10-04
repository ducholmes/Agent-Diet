"""Restricted file operations and execution of private configured commands."""
from __future__ import annotations

import difflib
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .container import RepairContainer
from .runtime import sdk_tool_api

PHASES = ("setup", "build", "target_test", "regression_test")
TOOL_NAMES = (
    "list_configured_commands", "run_configured_command", "list_files",
    "read_file", "search_text", "write_file", "edit_file", "inspect_workspace_diff",
)
WORKSPACE_TOOL_REGEX = r"^(?:" + "|".join(TOOL_NAMES) + r")$"


@dataclass(frozen=True, slots=True)
class WorkspaceCommandResult:
    output: str
    exit_code: int = 0
    timed_out: bool = False

    def render(self) -> str:
        return f"exit_code={self.exit_code} timed_out={self.timed_out}\n{self.output}"


class WorkspaceTools:
    def __init__(self, container: RepairContainer, *, execution_plan: dict,
                 timeout_seconds: int, output_limit_bytes: int):
        self.container = container
        self.workspace = container.workspace.resolve()
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes
        # Copy into immutable executor-owned values; caller mutations and file
        # edits cannot change the commands after construction.
        if set(execution_plan) != set(PHASES):
            raise ValueError("Execution plan must declare all four phases")
        self._protected_files: set[Path] = set()
        self._commands: dict[str, tuple[tuple[tuple[str, ...], str], ...]] = {}
        for phase in PHASES:
            commands = []
            for entry in execution_plan[phase]:
                argv, cwd = entry["argv"], entry["cwd"]
                if not isinstance(argv, (list, tuple)) or not argv or any(
                    not isinstance(arg, str) or "\0" in arg for arg in argv
                ):
                    raise ValueError("Configured argv must contain string arguments without NUL")
                if not isinstance(cwd, str):
                    raise ValueError("Configured cwd must be a string")
                self._path(cwd)
                commands.append((tuple(argv), cwd))
                # Protect existing files explicitly named by a command, including
                # shell-wrapper scripts. This is file protection, not shell-command
                # classification; execution always uses the private argv directly.
                tokens = list(argv)
                if len(argv) >= 3 and Path(argv[0]).name in {"sh", "bash", "dash"}:
                    try:
                        tokens.extend(shlex.split(argv[-1]))
                    except ValueError:
                        pass
                for token in tokens:
                    try:
                        relative = (self.workspace / cwd / token).resolve().relative_to(self.workspace)
                        file = self._path(relative.as_posix())
                        if file.is_file():
                            self._protected_files.add(file)
                    except (ValueError, OSError):
                        continue
            self._commands[phase] = tuple(commands)

    def _path(self, value: str, *, write: bool = False) -> Path:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("workspace_escape")
        if any(part == ".git" or part.startswith(".agent-diet") for part in path.parts):
            if not (not write and path.as_posix() == ".agent-diet.failure.log"):
                raise ValueError("protected_path")
        candidate = self.workspace
        for part in path.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError("symlink_access")
        candidate.resolve().relative_to(self.workspace)
        if write:
            if candidate in self._protected_files:
                raise ValueError("protected_command_file")
            parts = tuple(part.lower() for part in path.parts)
            name = path.name.lower()
            if (any(part in {"test", "tests", "testing", "fixtures", "testdata", "__tests__"} for part in parts)
                    or name.startswith(("test_", "test-"))
                    or name.endswith(("_test.py", "_test.go", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
                    or name == "failure.log"):
                raise ValueError("protected_test_or_artifact")
            if candidate.exists() and (not candidate.is_file() or candidate.stat().st_nlink != 1):
                raise ValueError("unsafe_write_target")
        if candidate.exists() and not (candidate.is_file() or candidate.is_dir()):
            raise ValueError("special_file_access")
        return candidate

    def _visible_files(self, root: Path):
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name != ".git" and not name.startswith(".agent-diet")
                             and not (Path(directory) / name).is_symlink())
            for name in sorted(files):
                relative = (Path(directory) / name).relative_to(self.workspace)
                try:
                    path = self._path(relative.as_posix())
                    if path.is_file():
                        yield path
                except ValueError:
                    continue

    def list_configured_commands(self) -> str:
        return json.dumps({phase: [
            {"index": index, "argv": list(argv), "cwd": cwd}
            for index, (argv, cwd) in enumerate(self._commands[phase])
        ] for phase in PHASES}, ensure_ascii=False, indent=2)

    def run_configured_command(self, phase: str, index: int) -> WorkspaceCommandResult:
        if phase not in self._commands or type(index) is not int or index < 0 or index >= len(self._commands[phase]):
            return WorkspaceCommandResult("blocked=true reason=unknown_configured_command", 126)
        argv, cwd = self._commands[phase][index]
        try:
            target = self._path(cwd)
            if not target.is_dir():
                raise ValueError("command_cwd_unavailable")
        except ValueError as exc:
            return WorkspaceCommandResult(f"blocked=true reason={exc}", 126)
        # No shell parsing of model input. Shell wrappers explicitly declared
        # by the trusted plan still run unchanged.
        return self._execute((self.container.runtime, "exec", "--workdir", str(target),
                              self.container.name, "timeout", str(self.timeout_seconds), *argv))

    def _execute(self, command: tuple[str, ...], *, env: dict | None = None) -> WorkspaceCommandResult:
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       errors="replace", timeout=self.timeout_seconds + 10,
                                       check=False, env=env)
            output, code = completed.stdout + completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as exc:
            def decode(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
            output, code = decode(exc.stdout) + decode(exc.stderr), 124
        return WorkspaceCommandResult(output[-self.output_limit_bytes:], code, code == 124)

    def list_files(self, path: str = ".") -> str:
        root = self._path(path)
        if not root.is_dir():
            raise ValueError("not_a_directory")
        output = []
        size = 0
        for file in self._visible_files(root):
            line = file.relative_to(self.workspace).as_posix()
            size += len(line) + 1
            if size > self.output_limit_bytes:
                output.append("[truncated; narrow the directory]")
                break
            output.append(line)
        return "\n".join(output)

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200) -> str:
        if start_line < 1 or not 1 <= max_lines <= 2000:
            raise ValueError("invalid_line_range")
        file = self._path(path)
        if not file.is_file():
            raise ValueError("not_a_file")
        output = []
        size = 0
        with file.open(encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                if number < start_line:
                    continue
                if number >= start_line + max_lines:
                    break
                text = f"{number}: {line.rstrip()}"
                size += len(text) + 1
                output.append(text[:self.output_limit_bytes])
                if size >= self.output_limit_bytes:
                    break
        return "\n".join(output)[:self.output_limit_bytes]

    def search_text(self, query: str, path: str = ".") -> str:
        if not query:
            raise ValueError("empty_search_query")
        root = self._path(path)
        files = self._visible_files(root) if root.is_dir() else (root,)
        output = []
        size = 0
        for file in files:
            if file.stat().st_size > 2_000_000:
                continue
            with file.open(encoding="utf-8", errors="replace") as stream:
                for number, line in enumerate(stream, 1):
                    if query in line:
                        text = f"{file.relative_to(self.workspace)}:{number}: {line.rstrip()}"
                        output.append(text[:self.output_limit_bytes])
                        size += len(text) + 1
                        if size >= self.output_limit_bytes:
                            return "\n".join(output)[:self.output_limit_bytes] + "\n[truncated]"
        return "\n".join(output)

    def write_file(self, path: str, content: str, expected_content: str | None = None) -> str:
        file = self._path(path, write=True)
        if file.exists():
            if expected_content is None or file.read_text(encoding="utf-8") != expected_content:
                raise ValueError("expected_content_mismatch: read the file before replacing it")
        elif expected_content is not None:
            raise ValueError("expected_content_mismatch: file does not exist")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
        return f"Updated {path}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        file = self._path(path, write=True)
        if not old_text:
            raise ValueError("empty_edit_target")
        content = file.read_text(encoding="utf-8")
        if content.count(old_text) != 1:
            raise ValueError("edit_target_must_match_exactly_once")
        file.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Updated {path}"

    def inspect_workspace_diff(self) -> WorkspaceCommandResult:
        # Fixed argv and disable repository-controlled external diff/textconv.
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        result = self._execute(("git", "-C", str(self.workspace), "diff", "--no-ext-diff",
                                "--no-textconv", "HEAD", "--"), env=env)
        if result.exit_code:
            return result
        untracked = self._execute(("git", "-C", str(self.workspace), "ls-files", "--others",
                                   "--exclude-standard", "-z"), env=env)
        if untracked.exit_code:
            return untracked
        output = result.output
        for name in untracked.output.split("\0"):
            if not name:
                continue
            file = self._path(name)
            if file.stat().st_size > self.output_limit_bytes:
                output += f"\nNew file: {name} [too large to preview]\n"
            else:
                content = file.read_text(encoding="utf-8", errors="replace")
                output += "".join(difflib.unified_diff([], content.splitlines(keepends=True),
                                                   fromfile="/dev/null", tofile=f"b/{name}"))
        return WorkspaceCommandResult(output[:self.output_limit_bytes])



def workspace_tool_specs(tools: WorkspaceTools) -> list[Any]:
    """Register explicit SDK actions; no action accepts shell argv or cwd."""
    api = sdk_tool_api()
    Action, Observation = api["Action"], api["Observation"]
    TextContent, ToolDefinition = api["TextContent"], api["ToolDefinition"]
    ToolExecutor, Tool = api["ToolExecutor"], api["Tool"]
    from pydantic import ConfigDict, Field

    class RestrictedAction(Action):
        model_config = ConfigDict(extra="forbid")

    class ListConfiguredCommandsAction(RestrictedAction):
        pass

    class RunConfiguredCommandAction(RestrictedAction):
        phase: Literal["setup", "build", "target_test", "regression_test"]
        index: int = Field(ge=0, strict=True, description="Zero-based command index")

    class ListFilesAction(RestrictedAction):
        path: str = "."

    class ReadFileAction(RestrictedAction):
        path: str
        start_line: int = Field(default=1, ge=1)
        max_lines: int = Field(default=200, ge=1, le=2000)

    class SearchTextAction(RestrictedAction):
        query: str = Field(min_length=1, description="Literal text to find")
        path: str = "."

    class WriteFileAction(RestrictedAction):
        path: str
        content: str
        expected_content: str | None = Field(default=None, description="Exact previous contents; required for existing files")

    class EditFileAction(RestrictedAction):
        path: str
        old_text: str = Field(min_length=1, description="Exact text occurring once in the file")
        new_text: str

    class InspectWorkspaceDiffAction(RestrictedAction):
        pass

    class WorkspaceObservation(Observation):
        output: str
        exit_code: int = 0
        timed_out: bool = False

        @property
        def to_llm_content(self):
            return [TextContent(text=WorkspaceCommandResult(self.output, self.exit_code, self.timed_out).render())]

    def register(name, action_type, description):
        method = getattr(tools, name)

        class Executor(ToolExecutor):
            def __call__(self, action, conversation=None):
                values = {field: getattr(action, field) for field in action_type.model_fields
                          if field not in Action.model_fields}
                try:
                    result = method(**values)
                    if isinstance(result, str):
                        result = WorkspaceCommandResult(result[:tools.output_limit_bytes])
                except (ValueError, OSError) as exc:
                    result = WorkspaceCommandResult(f"blocked=true reason={exc}", 126)
                return WorkspaceObservation(output=result.output, exit_code=result.exit_code, timed_out=result.timed_out)

        def create(cls, conv_state, **params):
            return [cls(description=description, action_type=action_type,
                        observation_type=WorkspaceObservation, executor=Executor())]

        class_name = "".join(part.title() for part in name.split("_")) + "Tool"
        Definition = type(class_name, (ToolDefinition[action_type, WorkspaceObservation],), {
            "__module__": __name__, "create": classmethod(create), "name": name,
        })
        registry_name = "AgentDiet_" + name
        api["register_tool"](registry_name, Definition)
        return Tool(name=registry_name)

    definitions = (
        (TOOL_NAMES[0], ListConfiguredCommandsAction, "Inspect the private permitted commands and zero-based indices."),
        (TOOL_NAMES[1], RunConfiguredCommandAction, "Execute exactly one configured command. The executor supplies argv and cwd."),
        (TOOL_NAMES[2], ListFilesAction, "Recursively list files within a workspace directory."),
        (TOOL_NAMES[3], ReadFileAction, "Read numbered lines from a workspace file."),
        (TOOL_NAMES[4], SearchTextAction, "Search workspace files for literal text."),
        (TOOL_NAMES[5], WriteFileAction, "Create or replace a production file. Supply exact expected_content for existing files. Tests and internal artifacts are protected."),
        (TOOL_NAMES[6], EditFileAction, "Replace exactly one matching text block in a production file."),
        (TOOL_NAMES[7], InspectWorkspaceDiffAction, "Inspect tracked source changes with a fixed Git diff command."),
    )
    return [register(*definition) for definition in definitions]
