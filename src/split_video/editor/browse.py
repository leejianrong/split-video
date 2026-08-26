"""Directory listing for the editor's file picker, scoped to a root directory.

The picker only ever needs to go *down* from wherever `split-video edit` was
pointed, so every lookup is bounds-checked against that root to rule out
`..`-escapes or an absolute path sneaking a client outside of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".m2ts", ".flv", ".wmv"}


class PathEscapesRootError(ValueError):
    """A requested relative path resolves outside the browse root."""


@dataclass(frozen=True)
class BrowseEntry:
    name: str
    path: str  # POSIX-style, relative to the root
    is_dir: bool


@dataclass(frozen=True)
class BrowseListing:
    cwd: str  # "" means the root itself
    parent: str | None  # None only when cwd is the root
    entries: list[BrowseEntry]


def _relative(path: Path, root: Path) -> str:
    return "" if path == root else path.relative_to(root).as_posix()


def resolve_within_root(root: Path, rel_path: str) -> Path:
    root = root.resolve()
    candidate = (root / rel_path.lstrip("/")).resolve() if rel_path else root
    if candidate != root and root not in candidate.parents:
        raise PathEscapesRootError(rel_path)
    return candidate


def list_directory(root: Path, rel_path: str = "") -> BrowseListing:
    root = root.resolve()
    target = resolve_within_root(root, rel_path)
    if not target.is_dir():
        raise NotADirectoryError(str(target))

    entries: list[BrowseEntry] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            entries.append(BrowseEntry(name=child.name, path=_relative(child, root), is_dir=True))
        elif child.suffix.lower() in VIDEO_EXTENSIONS:
            entries.append(BrowseEntry(name=child.name, path=_relative(child, root), is_dir=False))

    parent = None if target == root else _relative(target.parent, root)
    return BrowseListing(cwd=_relative(target, root), parent=parent, entries=entries)
