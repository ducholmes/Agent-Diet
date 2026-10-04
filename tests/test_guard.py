from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openhands_adapter.security.guard import CommandGuard

class GuardTests(unittest.TestCase):
    def test_allows_workspace_command(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(CommandGuard(Path(directory)).check("sed -n '1p' src/main.py").allowed)

    def test_rejects_parent_escape_and_sensitive_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); guard = CommandGuard(root / "repair", sensitive_roots=(root / "validation",))
            self.assertFalse(guard.check("cat ../validation/result.json").allowed)
            self.assertFalse(guard.check(f"cat {root / 'validation' / 'result.json'}").allowed)

    def test_rejects_benchmark_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(CommandGuard(Path(directory)).check("find /tmp -iname '*golden-patch*'").allowed)

if __name__ == "__main__": unittest.main()
