"""`ProjectStore` on local disk: the sidecar files next to the videos.

Split-point projects live in `<video>.split-video-project.json` right next to
the video; timeline projects live in `<root>/<name>.split-video-timeline.json`.
Both have to be files in the directory being edited, not a centralized path
like `~/.cache/`: `make dev` runs the editor in a `--rm`'d container with only
the video directory bind-mounted, so anything written elsewhere is wiped on
the next run. This is the only module that knows those file names.

Each project file has a lock file in a hidden `.split-video-locks/` directory next to it, named after it plus
`.lock` (`.split-video-locks/<video>.split-video-project.json.lock`), so no empty `.lock` files pile up beside the
videos; the directory is still inside the bind-mounted workspace.
A writer holds an exclusive advisory lock (`fcntl.flock`) on it across
read-check-write, so the revision compare-and-swap is atomic across processes
(the editor server, the CLI and the MCP server are separate processes editing
the same files). Lock files are empty, are never deleted (removing one would
break mutual exclusion), are not project files (they never show up in `list_timelines`) and are git-ignored. Readers don't lock: files
are replaced by rename, so a reader sees the old or the new file.
Where `flock` isn't available (Windows) or fails (some network filesystems) the
store falls back to a per-process lock: writers in one process still exclude
each other, writers in different processes then don't.

Deliberately separate from `local_media_store.py`, which holds *derived*
results that are safe to lose; these files hold what the user placed by hand.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from split_video import timeline
from split_video.store import (
    RevisionChange,
    SavedSegment,
    Versioned,
    announce_revision,
    announcing_write,
    check_base_revision,
)
from split_video.timeline import Project, ProjectExistsError, ProjectNotFoundError

try:  # POSIX only
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SPLIT_FORMAT_VERSION = 2
SPLIT_SUFFIX = ".split-video-project.json"
TIMELINE_SUFFIX = ".split-video-timeline.json"
LOCK_SUFFIX = ".lock"  # appended to a project file's name: the file `flock`ed around its writes
LOCK_DIR = ".split-video-locks"  # hidden directory next to the project file that holds its lock file

_process_locks: dict[str, threading.Lock] = {}
_process_locks_guard = threading.Lock()
_warned_no_flock = False


@contextmanager
def _write_lock(project_file: Path) -> Iterator[None]:
    """Exclusive write lock for one project file, across threads and (via `flock`) processes.

    Not re-entrant: don't take it again for the same project while holding it."""
    global _warned_no_flock
    lock_path = project_file.parent / LOCK_DIR / f"{project_file.name}{LOCK_SUFFIX}"
    key = os.path.abspath(lock_path)
    with _process_locks_guard:
        thread_lock = _process_locks.setdefault(key, threading.Lock())
    with thread_lock:
        fd: int | None = None
        if fcntl is not None:
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                if fd is not None:
                    os.close(fd)
                    fd = None
                if not _warned_no_flock:
                    _warned_no_flock = True
                    log.warning("can't lock %s (%s); writes are only serialised within this process", lock_path, exc)
        try:
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


def _revision_of(data: object) -> int:
    """The `revision` key of a parsed project file; 0 if it is absent or not a non-negative integer."""
    value = data.get("revision", 0) if isinstance(data, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _read_json(path: Path) -> Any:
    try:
        with path.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _write_json_atomically(path: Path, payload: object) -> None:
    """Temp file then rename, so a concurrent reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp"
    with tmp.open("w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)


class LocalProjectStore:
    """Projects as sidecar files under `root`.

    A split project's id is the video's path relative to `root` (POSIX
    separators); it may not escape `root`.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._peeked: dict[str, tuple[tuple[int, int, int], int]] = {}  # path -> ((mtime_ns, size, inode), revision)
        self._peek_lock = threading.Lock()

    # --- paths -----------------------------------------------------------------

    def _split_paths(self, project_id: str) -> tuple[Path, Path]:
        """The video's path and its sidecar's, for a project id."""
        rel = posixpath.normpath(project_id.lstrip("/")) if project_id.strip("/") else ""
        if not rel or rel == "." or rel == ".." or rel.startswith("../"):
            raise ValueError(f"invalid project id {project_id!r}")
        video = self.root / rel
        return video, video.parent / f"{video.name}{SPLIT_SUFFIX}"

    def split_sidecar_path(self, project_id: str) -> Path:
        return self._split_paths(project_id)[1]

    def video_timeline_path(self, project_id: str) -> Path:
        """The video's own timeline project: `<video name>.split-video-timeline.json` next to the video."""
        video, _ = self._split_paths(project_id)
        return video.parent / f"{video.name}{TIMELINE_SUFFIX}"

    def timeline_path(self, name: str) -> Path:
        timeline.validate_name(name)
        return self.root / f"{name}{TIMELINE_SUFFIX}"

    # --- split-point projects ----------------------------------------------------

    def _parse_split(self, data: Any) -> list[SavedSegment] | None:
        try:
            return [
                SavedSegment(
                    start=float(s["start"]),
                    end=float(s["end"]),
                    label=str(s.get("label", "")),
                    color=s.get("color"),
                    included=bool(s.get("included", True)),
                    export_name=s.get("export_name"),
                )
                for s in data["segments"]
            ]
        except (KeyError, TypeError, ValueError):
            return None

    def load_split(self, project_id: str) -> list[SavedSegment] | None:
        try:
            return self.load_split_versioned(project_id).value
        except ValueError:
            return None

    def load_split_versioned(self, project_id: str) -> Versioned[list[SavedSegment] | None]:
        try:
            data = _read_json(self.split_sidecar_path(project_id))
        except ValueError:
            return Versioned(None, 0)
        return Versioned(self._parse_split(data), _revision_of(data))

    def _write_split(self, video_name: str, path: Path, segments: list[SavedSegment], revision: int) -> None:
        payload = {
            "version": SPLIT_FORMAT_VERSION,
            "revision": revision,
            "source_file": video_name,
            "segments": [
                {
                    "start": s.start,
                    "end": s.end,
                    "label": s.label,
                    "color": s.color,
                    "included": s.included,
                    "export_name": s.export_name,
                }
                for s in segments
            ],
        }
        _write_json_atomically(path, payload)

    def save_split(self, project_id: str, segments: list[SavedSegment], base_revision: int | None = None) -> int:
        video, path = self._split_paths(project_id)
        with announcing_write("split", project_id, self), _write_lock(path):
            current = _revision_of(_read_json(path))
            check_base_revision("split project", project_id, current, base_revision)
            self._write_split(video.name, path, segments, current + 1)
        announce_revision(RevisionChange("split", project_id, current + 1, self))
        return current + 1

    def update_split(
        self,
        project_id: str,
        change: Callable[[list[SavedSegment] | None], list[SavedSegment]],
        base_revision: int | None = None,
    ) -> Versioned[list[SavedSegment]]:
        video, path = self._split_paths(project_id)
        with announcing_write("split", project_id, self), _write_lock(path):
            data = _read_json(path)
            current = _revision_of(data)
            check_base_revision("split project", project_id, current, base_revision)
            segments = change(self._parse_split(data))
            self._write_split(video.name, path, segments, current + 1)
        announce_revision(RevisionChange("split", project_id, current + 1, self))
        return Versioned(segments, current + 1)

    # --- a video's timeline project ----------------------------------------------

    def _read_video_timeline(self, project_id: str) -> tuple[Path, Project | None, int]:
        """The video's timeline file's path, its project (None if absent or unusable) and the revision writes are
        checked against: the file's own, or the legacy split file's while the file is not there."""
        path = self.video_timeline_path(project_id)
        data = _read_json(path)
        if not isinstance(data, dict):  # missing, or not JSON at all: the legacy project (if any) is the state
            return path, None, _revision_of(_read_json(self.split_sidecar_path(project_id)))
        revision = _revision_of(data)
        try:
            return path, replace(timeline.from_dict(data), revision=revision), revision
        except (KeyError, TypeError, ValueError, AttributeError):
            return path, None, revision

    def load_video_timeline(self, project_id: str) -> Versioned[Project | None]:
        try:
            _, project, revision = self._read_video_timeline(project_id)
        except ValueError:  # an id that can't be a project id
            return Versioned(None, 0)
        return Versioned(project, revision)

    def update_video_timeline(
        self, project_id: str, change: Callable[[Project | None], Project], base_revision: int | None = None
    ) -> Versioned[Project]:
        path = self.video_timeline_path(project_id)
        with announcing_write("split", project_id, self), _write_lock(path):
            _, stored, current = self._read_video_timeline(project_id)
            check_base_revision("split project", project_id, current, base_revision)
            new = change(stored)
            self._write_timeline(path, new, current + 1)
        announce_revision(RevisionChange("split", project_id, current + 1, self))
        return Versioned(replace(new, revision=current + 1), current + 1)

    # --- timeline projects -------------------------------------------------------

    def list_timelines(self) -> list[str]:
        # A file `<video>.split-video-timeline.json` next to its video is that video's own project (see
        # `load_video_timeline`), not a named one.
        names = (p.name.removesuffix(TIMELINE_SUFFIX) for p in self.root.glob(f"*{TIMELINE_SUFFIX}"))
        return sorted(n for n in names if not (self.root / n).is_file())

    def timeline_exists(self, name: str) -> bool:
        return self.timeline_path(name).exists()

    def load_timeline(self, name: str) -> Project:
        """The project, or an empty one if the file is missing or unreadable —
        a damaged file should open as a blank project, not crash."""
        path = self.timeline_path(name)  # validates the name; a bad one is an error, not a blank project
        return self._read_timeline(path)

    @staticmethod
    def _read_timeline(path: Path) -> Project:
        data = _read_json(path)
        revision = _revision_of(data)  # a damaged file keeps its revision, so revisions never go backwards
        try:
            return replace(timeline.from_dict(data), revision=revision)
        except (KeyError, TypeError, ValueError, AttributeError):
            return replace(timeline.empty_project(), revision=revision)

    def load_existing_timeline(self, name: str) -> Project:
        if not self.timeline_exists(name):
            raise ProjectNotFoundError(f"no project named {name!r}")
        return self.load_timeline(name)

    @staticmethod
    def _write_timeline(path: Path, project: Project, revision: int) -> None:
        body = timeline.to_dict(project)
        payload = {"version": body.pop("version"), "revision": revision, **body}
        _write_json_atomically(path, payload)

    def save_timeline(self, name: str, project: Project, base_revision: int | None = None) -> int:
        path = self.timeline_path(name)
        with announcing_write("timeline", name, self), _write_lock(path):
            current = _revision_of(_read_json(path))
            check_base_revision("timeline project", name, current, base_revision)
            self._write_timeline(path, project, current + 1)
        announce_revision(RevisionChange("timeline", name, current + 1, self))
        return current + 1

    def update_timeline(
        self, name: str, change: Callable[[Project], Project], base_revision: int | None = None
    ) -> Project:
        path = self.timeline_path(name)
        with announcing_write("timeline", name, self), _write_lock(path):
            if not path.exists():
                raise ProjectNotFoundError(f"no project named {name!r}")
            stored = self._read_timeline(path)
            check_base_revision("timeline project", name, stored.revision, base_revision)
            new = change(stored)
            self._write_timeline(path, new, stored.revision + 1)
        announce_revision(RevisionChange("timeline", name, stored.revision + 1, self))
        return replace(new, revision=stored.revision + 1)

    def create_timeline(self, name: str) -> Project:
        path = self.timeline_path(name)
        with announcing_write("timeline", name, self), _write_lock(path):
            if path.exists():
                raise ProjectExistsError(f"project {name!r} already exists")
            project = timeline.empty_project()
            self._write_timeline(path, project, 0)
        announce_revision(RevisionChange("timeline", name, 0, self))
        return project

    # --- noticing other processes' writes ------------------------------------------

    def peek_revisions(self, split_ids: Iterable[str] = ()) -> dict[tuple[str, str], int]:
        found: dict[tuple[str, str], int] = {}
        for name in self.list_timelines():
            found[("timeline", name)] = self._peek_revision(self.root / f"{name}{TIMELINE_SUFFIX}")
        for project_id in split_ids:
            try:
                unified = self.video_timeline_path(project_id)
                legacy = self.split_sidecar_path(project_id)
            except ValueError:
                continue
            found[("split", project_id)] = self._peek_revision(unified if unified.exists() else legacy)
        return found

    def _peek_revision(self, path: Path) -> int:
        """The revision in `path`, re-reading the file only when its size, mtime or inode changed since
        the last peek (a timeline file can be large; this runs about once a second)."""
        try:
            st = path.stat()
        except OSError:
            return 0
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        key = str(path)
        with self._peek_lock:
            cached = self._peeked.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        revision = _revision_of(_read_json(path))
        with self._peek_lock:
            self._peeked[key] = (stamp, revision)
        return revision
