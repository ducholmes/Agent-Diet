#!/usr/bin/env bash
# Create the isolated interpreter used by the Agent Diet/OpenHands adapter.
#
# Optionally pass a Python executable, for example:
#   ./setup_openhands.sh /path/to/python3.11
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${1:-python3}"

if ! command -v "$python_bin" >/dev/null 2>&1 && [[ ! -x "$python_bin" ]]; then
    echo "Python executable not found: $python_bin" >&2
    exit 1
fi

"$python_bin" -m venv "$here/.venv-openhands"
"$here/.venv-openhands/bin/python" -m pip install --requirement "$here/requirements-openhands.lock"
"$here/.venv-openhands/bin/python" -m pip check
