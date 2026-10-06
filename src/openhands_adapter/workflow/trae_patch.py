"""The source unstaged tracked diff and original test filter."""
from pathlib import Path
import subprocess
import sys
import re
from ..compat.audit import byte_hash
from ..events import emit

TIME_OUT_LABEL = " seconds. Partial output:"

def remove_patches_to_tests(model_patch):
    """
    Remove any changes to the tests directory from the provided patch.
    This is to ensure that the model_patch does not disturb the repo's
    tests when doing acceptance testing with the `test_patch`.
    """
    lines = model_patch.splitlines(keepends=True)
    filtered_lines = []
    is_tests = False

    for line in lines:
        if line.startswith("diff --git a/"):
            pieces = line.split()
            to = pieces[-1]
            if to.startswith("b/") and any(
                x in to for x in [
                    "/test/", "/tests/", "/testing/", "/test_",
                    ".tests.", ".test.", "_test_", "_tests_", "_test.", "_tests.", ".spec.ts",
                    "/tox.ini", "/Cargo.lock", "/package.json", "/package-lock.json", "/pom.xml",
                ]
            ):
                is_tests = True
            else:
                is_tests = False

        if not is_tests:
            filtered_lines.append(line)

    return "".join(filtered_lines)

DIFF_COMMAND = ("git", "--no-pager", "diff", "--ignore-submodules=all")


def capture_source_diff(workspace: Path) -> str:
    """Execute the frozen helper in a child, preserving both printed streams."""
    helper = Path(__file__).resolve().parents[3] / 'artifact/artifact/code/trae_agent/tools/get_diff.py'
    try:
        result = subprocess.run((sys.executable, str(helper), '-p', str(workspace)),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        output = result.stdout.decode(errors='replace')
        if 'git diff error:  ' in output:
            emit('trae_capture_error', origin='host_helper', diagnostic=output)
        return output
    except Exception as error:
        emit('trae_capture_error', origin='host_exec', error_type=type(error).__name__)
        return ''


def capture_filtered_patch(workspace: Path) -> str:
    return remove_patches_to_tests(capture_source_diff(workspace))


def validate_snapshot(snapshot: object, profile: str) -> dict:
    """Validate a terminal contract without rewriting its representation."""
    if not isinstance(snapshot, dict):
        raise ValueError("contract snapshot must be an object")
    if type(snapshot.get('schema_version')) is not int or snapshot['schema_version'] != 1:
        raise ValueError('contract schema version invalid')
    if snapshot.get('capture_retention', 'raw') not in {'raw', 'hash-only'}:
        raise ValueError('contract capture retention invalid')
    if not isinstance(snapshot.get('run_id'), str) or not snapshot['run_id']:
        raise ValueError('contract run identity missing')
    hash_only = snapshot.get('capture_retention') == 'hash-only'
    required = {"profile", "gen", "turns", "patch", "patch_origin"}
    required |= {'original_patch_bytes_sha256', 'filtered_patch_bytes_sha256', 'has_filtered_patch'} if hash_only else {'original_patch', 'filtered_patch'}
    if required - snapshot.keys():
        raise ValueError("contract snapshot missing fields: " + ", ".join(sorted(required - snapshot.keys())))
    if snapshot["profile"] != profile:
        raise ValueError("contract profile mismatch")
    cap = {"trae_verified": 50, "trae_multiswe": 100}[profile]
    turns = snapshot["turns"]
    if type(turns) is not int or not 0 <= turns <= cap:
        raise ValueError("contract turn range invalid")
    if snapshot["gen"] not in {"task_done", "turn_capped", "task_failed", "generation_error"}:
        raise ValueError("contract generation status invalid")
    if snapshot["gen"] == "turn_capped" and turns != cap:
        raise ValueError("contract cap turn mismatch")
    if snapshot["gen"] == "task_done" and turns == 0:
        raise ValueError("contract task_done requires a repair turn")
    if not isinstance(snapshot['patch'], str):
        raise ValueError('contract patch must be a string')
    if hash_only:
        if any(key in snapshot for key in ('original_patch', 'filtered_patch', 'steps', 'user_message')):
            raise ValueError('hash-only snapshot contains private raw content')
        for key in ('original_patch_bytes_sha256', 'filtered_patch_bytes_sha256'):
            if not isinstance(snapshot[key], str) or re.fullmatch('[0-9a-f]{64}', snapshot[key]) is None:
                raise ValueError('contract capture hash invalid')
        if type(snapshot['has_filtered_patch']) is not bool or bool(snapshot['patch'].strip()) != snapshot['has_filtered_patch']:
            raise ValueError('contract patch emptiness mismatch')
        if snapshot['patch'].strip() and byte_hash(snapshot['patch'].encode('utf-8')) != snapshot['filtered_patch_bytes_sha256']:
            raise ValueError('contract filtered/submission hash mismatch')
    else:
        if any(not isinstance(snapshot[key], str) for key in ('original_patch', 'filtered_patch')):
            raise ValueError('contract patch fields must be strings')
        filtered = remove_patches_to_tests(snapshot['original_patch'])
        if filtered != snapshot['filtered_patch']:
            raise ValueError('contract raw/filter mismatch')
        if snapshot['patch'] != filtered and not (not filtered.strip() and snapshot['patch'] == ''):
            raise ValueError('contract submission mismatch')
    if snapshot["gen"] == "task_done" and not snapshot["patch"].strip():
        raise ValueError("contract task_done has an empty patch")
    expected_origin = "error_wip" if snapshot["gen"] == "generation_error" else "terminal_snapshot"
    if snapshot["patch_origin"] != expected_origin:
        raise ValueError("contract patch origin mismatch")
    return snapshot
