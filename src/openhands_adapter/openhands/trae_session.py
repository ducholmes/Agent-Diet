"""Trae outer shell, with structured container argv and a compatible PS1."""
from __future__ import annotations
from pathlib import Path
import shlex
import subprocess
import time
import os
import struct
import pexpect
from ..events import emit
from .container import RepairContainer

TOOL_ROOT = "/home/swe-bench/tools/claude_tools"
PYTHON = "/home/swe-bench/conda_envs/py312/bin/python3"
INITIAL_PROMPT_TIMEOUT = 10
OUTER_TIMEOUT = 180


def stage_tools(container: RepairContainer) -> None:
    """Copy only the reviewed wrappers; history/logs stay container-private."""
    prefix = (container.runtime, "exec", container.name)
    subprocess.run((*prefix, "python3", "--version"), check=True, capture_output=True, timeout=10)
    subprocess.run((*prefix, "mkdir", "-p", TOOL_ROOT, str(Path(PYTHON).parent)), check=True, capture_output=True, timeout=10)
    runtime = Path(__file__).resolve().parents[1] / "compat" / "tool_runtime"
    for name in ("base.py", "run.py", "bash.py", "edit.py", "execute_bash.py", "execute_str_replace_editor.py", "timeout_compat.py"):
        subprocess.run((container.runtime, "cp", str(runtime / name), f"{container.name}:{TOOL_ROOT}/{name}"), check=True, capture_output=True, timeout=10)
    source = Path(__file__).resolve().parents[3] / "artifact/artifact/code/trae_agent/tools/get_diff.py"
    subprocess.run((container.runtime, "cp", str(source), f"{container.name}:{TOOL_ROOT}/get_diff.py"),
                   check=True, capture_output=True, timeout=10)
    subprocess.run((*prefix, "git", "config", "--global", "--add", "safe.directory", str(container.workspace)),
                   check=True, capture_output=True, timeout=10)
    # This command is fixed infrastructure, never model-provided host shell text.
    subprocess.run((*prefix, "/bin/sh", "-c", f'ln -sf "$(command -v python3)" {PYTHON}'), check=True, capture_output=True, timeout=10)


class TraeSandbox:
    def __init__(self, container: RepairContainer):
        self.container = container
        self.shell = None
        self.shell_ready_ts = 0

    def start_shell(self):
        if self.shell is not None:
            self.shell.close(force=True)
        self.shell = pexpect.spawn(
            self.container.runtime,
            ["exec", "-it", "-e", r"PS1=root@trae:\w# ", self.container.name,
             "/bin/bash", "--noprofile", "--norc"],
            maxread=200000,
        )
        self.shell.expect([r"\$ ", r"# "], timeout=INITIAL_PROMPT_TIMEOUT)

    def get_session(self):
        self.start_shell()
        return TraeShellSession(self)

    def _exec_diff_helper(self):
        """Docker exec API, matching exec_run's merged stdout/stderr output.

        HTTPX is already pinned for the SDK transport. Using the exec API keeps
        inner exit codes distinct from runtime API failures without shell markers
        or interpreting Docker CLI diagnostic strings.
        """
        import httpx
        host = os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock')
        if not host.startswith('unix://'):
            raise RuntimeError('Exact diff capture requires a local Unix Docker endpoint')
        with httpx.Client(transport=httpx.HTTPTransport(uds=host[len('unix://'):]),
                          base_url='http://docker', timeout=60) as client:
            response = client.post(f'/containers/{self.container.name}/exec', json={
                'AttachStdout': True, 'AttachStderr': True, 'AttachStdin': False,
                'Tty': False, 'Privileged': False, 'User': '',
                'Cmd': [PYTHON, f'{TOOL_ROOT}/get_diff.py', '-p', str(self.container.workspace)]})
            response.raise_for_status()
            exec_id = response.json()['Id']
            response = client.post(f'/exec/{exec_id}/start', json={'Detach': False, 'Tty': False})
            response.raise_for_status()
            frames = response.content
            output = bytearray()
            offset = 0
            while offset < len(frames):
                if len(frames)-offset < 8:
                    raise RuntimeError('Incomplete Docker exec frame')
                stream, length = frames[offset], struct.unpack('>I', frames[offset+4:offset+8])[0]
                offset += 8
                if stream not in (1, 2) or offset+length > len(frames):
                    raise RuntimeError('Invalid Docker exec frame')
                output.extend(frames[offset:offset+length])
                offset += length
            # A helper's nonzero exit is not an exec exception. The source
            # get_diff_result ignores ExecResult.exit_code too.
            return bytes(output)

    def get_diff(self):
        for attempt in range(1, 4):
            try:
                output = self._exec_diff_helper().decode(errors='replace')
                if any(line.startswith('git diff error:  ') for line in output.splitlines()):
                    emit('trae_capture_error', origin='container_helper', diagnostic=output)
                return output
            except Exception as error:
                emit('trae_capture_error', origin='container_exec', attempt=attempt,
                     error_type=type(error).__name__)
                time.sleep(5)
        return ''

class TraeShellSession:
    def __init__(self, sandbox):
        self.sandbox = sandbox
    def execute(self, command, timeout=180):
        delay = self.sandbox.shell_ready_ts - time.time()
        if delay>0:
            time.sleep(delay)

        try:
            if command[-1] != '&':
                self.sandbox.shell.sendline(command + " && sleep 0.5")
            else:
                self.sandbox.shell.sendline(command)
            before = ''
            try_limit = 5
            current_try = 0
            self.sandbox.shell.before = b''
            self.sandbox.shell.after = b''
            self.sandbox.shell.buffer = b''
            time.sleep(.5)
            self.sandbox.shell.expect([r'swe-bench@.*:.*\$ ', r'root@.*:.*# '], timeout)
            output = self.sandbox.shell.before.decode('utf-8', errors='replace') + self.sandbox.shell.after.decode('utf-8', errors='replace') + self.sandbox.shell.buffer.decode('utf-8', errors='replace')

            #output = output.rpartition('')
            output_lines = output.split('\r\n')
            if len(output_lines) > 1:
                output_lines = output_lines[1:-1]
            # result_message = '### Observation: ' + '\n'.join(output_lines)
            result_message = '\n'.join(output_lines).replace("\x1b[?2004l\r", "")
            # truncation_length = 5000
            # if len(result_message) > truncation_length:
            #     return result_message[:truncation_length] + "\n...[Truncation]"
            return result_message
        except pexpect.TIMEOUT:
            partial_output = ''
            if isinstance(self.sandbox.shell.before, bytes):
                partial_output += self.sandbox.shell.before.decode('utf-8', errors='replace')
            if isinstance(self.sandbox.shell.after, bytes):
                partial_output += self.sandbox.shell.after.decode('utf-8', errors='replace')
            if isinstance(self.sandbox.shell.buffer, bytes):
                partial_output += self.sandbox.shell.buffer.decode('utf-8', errors='replace')
            partial_output_lines = partial_output.split('\n')
            if len(partial_output_lines) > 1:
                partial_output_lines = partial_output_lines[1:-1]
                partial_output = '\n'.join(partial_output_lines)
            return f"Command timed out after {timeout} seconds. Partial output:\n + {partial_output}"
    def close(self):
        if self.sandbox.shell:
            try:
                self.sandbox.shell.sendline('exit')
                self.sandbox.shell.expect(pexpect.EOF)
            except pexpect.TIMEOUT:
                pass
            self.sandbox.shell.close(force=True)
            self.sandbox.shell = None
