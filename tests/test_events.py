from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.events import configure, emit, read_events


class EventTests(unittest.TestCase):
    def test_events_are_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            configure(path, reset=True)
            emit("agent_action", tool="workspace_shell", action={"command": "git diff"})
            events = read_events(path)
            self.assertEqual(events[0]['type'], 'agent_action')
            for line in path.read_text(encoding="utf-8").splitlines():
                json.loads(line)
            configure(None)


if __name__ == "__main__":
    unittest.main()
