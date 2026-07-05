"""Workspace — an ephemeral temp dir per run with path confinement.

Starter files are copied in, all edits + tests run here, and it is torn down
afterwards. Nothing touches the repo or the host outside ``root``.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


class PathEscapeError(Exception):
    """Raised when a requested path would escape the workspace root."""


class Workspace:
    def __init__(self, root: str | Path | None = None, prefix: str = "codeswarm-") -> None:
        if root is None:
            self._owns_root = True
            self.root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
        else:
            self._owns_root = False
            self.root = Path(root).resolve()
            self.root.mkdir(parents=True, exist_ok=True)

    # -- path confinement -------------------------------------------------
    def resolve(self, relpath: str) -> Path:
        """Resolve a workspace-relative path, refusing anything that escapes root."""
        candidate = (self.root / relpath).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PathEscapeError(f"path escapes sandbox: {relpath!r}")
        return candidate

    # -- file ops ---------------------------------------------------------
    def write_file(self, relpath: str, content: str) -> Path:
        target = self.resolve(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read_file(self, relpath: str) -> str:
        return self.resolve(relpath).read_text(encoding="utf-8")

    def exists(self, relpath: str) -> bool:
        try:
            return self.resolve(relpath).exists()
        except PathEscapeError:
            return False

    def list_dir(self, relpath: str = ".") -> list[str]:
        target = self.resolve(relpath)
        if not target.exists():
            raise FileNotFoundError(relpath)
        return sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())

    def write_files(self, files: dict[str, str]) -> None:
        """Write a batch of {relpath: content} starter files."""
        for relpath, content in files.items():
            self.write_file(relpath, content)

    def snapshot_files(self) -> dict[str, str]:
        """Return a {relpath: content} snapshot of all UTF-8 files under root."""
        snap: dict[str, str] = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                try:
                    rel = p.relative_to(self.root).as_posix()
                    snap[rel] = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
        return snap

    # -- lifecycle --------------------------------------------------------
    def cleanup(self) -> None:
        """Remove the workspace tree (only if we created the temp dir)."""
        if self._owns_root and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()
