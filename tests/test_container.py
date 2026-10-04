from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openhands_adapter.openhands.container import start

class ContainerTests(unittest.TestCase):
    def test_repair_container_is_hardened_and_network_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory, patch("openhands_adapter.openhands.container.subprocess.run") as run:
            container = start(Path(directory), image="prepared:image")
        command = run.call_args_list[1].args[0]
        self.assertIn("--network=none", command); self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command); self.assertIn("--security-opt=no-new-privileges", command)
        self.assertIn(f"type=bind,src={Path(directory).resolve()},dst={Path(directory).resolve()}", command)
        self.assertEqual(container.image, "prepared:image")

if __name__ == "__main__": unittest.main()
