"""Creation and lifetime of isolated repair and validation copies."""
from __future__ import annotations
import shutil, tempfile
import os
import stat
from dataclasses import dataclass
from pathlib import Path

def remove_workspace(root: Path) -> None:
    """Remove temporary copies, repairing read-only directories when possible.

    Never swallow cleanup failures: foreign-owned files must be reported.
    Directory write permission is enough to delete even read-only files.
    """
    if not root.exists():
        return
    for directory, _, _ in os.walk(root, followlinks=False):
        path = Path(directory)
        mode = path.stat().st_mode
        if not mode & stat.S_IWUSR or not mode & stat.S_IXUSR:
            path.chmod(mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    shutil.rmtree(root)

@dataclass(slots=True)
class Workspaces:
    root: Path; repair: Path; validation: Path; keep: bool = False
    def cleanup(self) -> None:
        if not self.keep: remove_workspace(self.root)

def create_workspaces(source: Path, *, parent: Path | None = None, keep: bool = False) -> Workspaces:
    root = Path(tempfile.mkdtemp(prefix="agent-diet-", dir=parent))
    repair, validation = root / "repair", root / "validation"
    try:
        # Older runs may have left this executor config in the source tree.
        # Neither workspace should receive it; commands arrive through the worker.
        ignore = shutil.ignore_patterns(".agent-diet.repair-config.json")
        shutil.copytree(source, repair, symlinks=True, ignore=ignore)
        shutil.copytree(source, validation, symlinks=True, ignore=ignore)
        if (
            repair.resolve() == validation.resolve()
            or repair.resolve() == source.resolve()
            or validation.resolve() == source.resolve()
        ):
            raise RuntimeError("Workspaces are not isolated copies")
        return Workspaces(root, repair, validation, keep)
    except BaseException:
        # The caller cannot clean this directory if setup raises before it gets
        # the Workspaces object, so remove partial copies here.
        remove_workspace(root)
        raise
