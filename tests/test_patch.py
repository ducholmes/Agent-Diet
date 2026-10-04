from __future__ import annotations
import sys, tempfile, unittest, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openhands_adapter.workflow.patch import PatchError, apply_patch, assert_safe_patch, capture_patch, create_baseline
class PatchTests(unittest.TestCase):
    def test_patch_applies_inside_parent_repository_without_local_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(("git", "init", str(root)), check=True, capture_output=True)
            repair, validation = root / "repair", root / "output" / "validation"
            repair.mkdir()
            validation.mkdir(parents=True)
            (repair / "a.txt").write_text("old\n")
            (validation / "a.txt").write_text("old\n")
            create_baseline(repair)
            (repair / "a.txt").write_text("new\n")
            apply_patch(validation, capture_patch(repair))
            self.assertEqual((validation / "a.txt").read_text(), "new\n")
            self.assertFalse((root / "a.txt").exists())

    def test_captured_patch_applies_to_clean_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repair, validation = root / "repair", root / "validation"
            repair.mkdir(); validation.mkdir(); (repair / "a.txt").write_text("old\n"); (validation / "a.txt").write_text("old\n")
            create_baseline(repair); create_baseline(validation); (repair / "a.txt").write_text("new\n")
            apply_patch(validation, capture_patch(repair)); self.assertEqual((validation / "a.txt").read_text(), "new\n")
    def test_protected_path_is_rejected(self):
        with self.assertRaises(PatchError): assert_safe_patch(b"diff --git a/.git/x b/.git/x\n--- a/.git/x\n+++ b/.git/x\n")
if __name__ == "__main__": unittest.main()
