"""Where projects live: the `ProjectStore` interface and an in-memory version.

The engine (`project.py`, `timeline_service.py`, the editor routes, CLI and
MCP server) only ever reads and writes projects through a `ProjectStore`, so
it never builds a project file path itself. `local_store.LocalProjectStore`
keeps today's on-disk layout (a sidecar next to each video for split
projects, `<name>.split-video-timeline.json` files in the workspace root for
timeline projects); a hosted deployment would supply a database-backed
implementation of the same interface. `InMemoryProjectStore` is the
reference implementation and what the unit tests run the engine against.

Source media, written outputs and the derived caches are not part of this
interface: they live behind `media_store.MediaStore`.

Revisions: every project carries a revision, a non-negative integer. A project
that was never saved (or whose file predates revisions) is revision 0 and every
successful write increments it by exactly 1. Writes take an optional
`base_revision`: given, it must equal the stored revision or the write fails
with `RevisionConflictError` and nothing changes (compare-and-swap); omitted,
the write is applied to whatever is stored. Either way the read-check-write is
atomic, including across processes for the local store. `update_*` is the
atomic read-modify-write that every edit goes through, so two writers that
don't pass a base revision still can't lose each other's changes.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Generic, Literal, Protocol, TypeVar, runtime_checkable

from split_video import timeline
from split_video.errors import RevisionConflictError
from split_video.timeline import Project, ProjectExistsError, ProjectNotFoundError

T = TypeVar("T")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SavedSegment:
    start: float
    end: float
    # Added in format version 2 (#19/#18) — all optional so a version-1 file
    # (just start/end) still loads fine, with these at their defaults.
    label: str = ""
    color: str | None = None
    included: bool = True
    export_name: str | None = None


@dataclass(frozen=True)
class Versioned(Generic[T]):
    """A stored value together with the revision it was read at (or written as)."""

    value: T
    revision: int


# --- revision change hook ------------------------------------------------------
# The one place a revision bump is announced. Both stores call `announce_revision` after a write
# has been committed and the project's lock released; whatever wants to react (SPL-37's "project
# revision changed" event stream) subscribes here. Listeners run on the writer's thread and must
# not block or raise. Only writes made in this process are seen: another process (the CLI, the
# MCP server) editing the same files announces in its own process.


@dataclass(frozen=True)
class RevisionChange:
    kind: Literal["split", "timeline"]
    project_id: str  # the video's id for a split project, the name for a timeline
    revision: int  # the new revision
    # The store instance that wrote it, so a listener can tell its own store's writes from another
    # instance's in the same process (SPL-37 labels the former `local`, the latter `external`).
    origin: object = field(default=None, compare=False, repr=False)


RevisionListener = Callable[[RevisionChange], None]
_listeners: list[RevisionListener] = []
_listeners_lock = threading.Lock()


def subscribe_revisions(listener: RevisionListener) -> Callable[[], None]:
    """Call `listener` after every committed write; returns a function that unsubscribes it."""
    with _listeners_lock:
        _listeners.append(listener)

    def unsubscribe() -> None:
        with _listeners_lock:
            if listener in _listeners:
                _listeners.remove(listener)

    return unsubscribe


def announce_revision(change: RevisionChange) -> None:
    with _listeners_lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(change)
        except Exception:
            log.exception("revision listener failed")


def check_base_revision(kind: str, project_id: str, current: int, base_revision: int | None) -> None:
    """Raise `RevisionConflictError` unless `base_revision` is None or equals `current`."""
    if base_revision is not None and base_revision != current:
        raise RevisionConflictError(project_id, current, base_revision, kind)


@runtime_checkable
class ProjectStore(Protocol):
    """Load and save split-point projects and timeline projects by id."""

    # --- split-point projects, keyed by the video's root-relative path --------

    def load_split(self, project_id: str) -> list[SavedSegment] | None:
        """The saved segments for the video `project_id`, or None if there
        aren't any or they can't be read. Never raises for a missing or
        corrupt project: that falls back to fresh detection."""
        ...

    def load_split_versioned(self, project_id: str) -> Versioned[list[SavedSegment] | None]:
        """Like `load_split`, with the stored revision (0 if nothing is saved)."""
        ...

    def save_split(self, project_id: str, segments: list[SavedSegment], base_revision: int | None = None) -> int:
        """Replace the saved segments for `project_id` and return the new revision. Atomic: a
        concurrent `load_split` sees the old or the new list, never a partial one. With
        `base_revision`, raises `RevisionConflictError` (writing nothing) if it isn't the stored one."""
        ...

    def update_split(
        self,
        project_id: str,
        change: Callable[[list[SavedSegment] | None], list[SavedSegment]],
        base_revision: int | None = None,
    ) -> Versioned[list[SavedSegment]]:
        """Atomic read-modify-write: `change` gets the stored segments (None if nothing is saved) and
        returns the new ones, which are saved as the next revision. Nothing is written if `change`
        raises. Holds the project's write lock while `change` runs, so it must not call back into
        the store for the same project."""
        ...

    # --- timeline projects, keyed by name -------------------------------------

    def list_timelines(self) -> list[str]:
        """Names of all timeline projects, sorted."""
        ...

    def timeline_exists(self, name: str) -> bool: ...

    def load_timeline(self, name: str) -> Project:
        """The project `name`, stamped with its stored revision (`Project.revision`); an empty
        revision-0 project if it is missing or damaged (use `timeline_exists` or
        `load_existing_timeline` to tell)."""
        ...

    def load_existing_timeline(self, name: str) -> Project:
        """Like `load_timeline`, but raises `ProjectNotFoundError` if `name`
        doesn't exist."""
        ...

    def save_timeline(self, name: str, project: Project, base_revision: int | None = None) -> int:
        """Replace timeline project `name`, atomically, and return the new revision. With
        `base_revision`, raises `RevisionConflictError` (writing nothing) if it isn't the stored one."""
        ...

    def update_timeline(
        self, name: str, change: Callable[[Project], Project], base_revision: int | None = None
    ) -> Project:
        """Atomic read-modify-write of an existing project: `change` gets the stored project and
        returns the new one, which is saved as the next revision and returned (stamped with it).
        `ProjectNotFoundError` if `name` doesn't exist; nothing is written if `change` raises. The
        project's write lock is held while `change` runs."""
        ...

    def create_timeline(self, name: str) -> Project:
        """Create an empty timeline project at revision 0; `ProjectExistsError` if `name`
        is taken. Atomic with respect to other creators."""
        ...

    def peek_revisions(self, split_ids: Iterable[str] = ()) -> dict[tuple[str, str], int]:
        """Cheaply read the current revision of every timeline project and of each split project in
        `split_ids`, keyed `("timeline", name)` / `("split", project_id)`. A split project with
        nothing saved is revision 0; an id the store can't use is left out. Never raises for
        damaged files. This is how a server notices edits made by other processes (the CLI, the MCP
        server), which `announce_revision` can't tell it about; it takes no write lock."""
        ...


class InMemoryProjectStore:
    """A `ProjectStore` that keeps everything in dicts; nothing touches disk."""

    def __init__(self) -> None:
        self._splits: dict[str, tuple[list[SavedSegment], int]] = {}
        self._timelines: dict[str, tuple[Project, int]] = {}
        # Re-entrant so `change` callbacks can read (never write) the project they are editing.
        self._lock = threading.RLock()

    # --- split projects ---------------------------------------------------------

    def load_split(self, project_id: str) -> list[SavedSegment] | None:
        return self.load_split_versioned(project_id).value

    def load_split_versioned(self, project_id: str) -> Versioned[list[SavedSegment] | None]:
        with self._lock:
            found = self._splits.get(project_id)
        return Versioned(None, 0) if found is None else Versioned(list(found[0]), found[1])

    def save_split(self, project_id: str, segments: list[SavedSegment], base_revision: int | None = None) -> int:
        with self._lock:
            revision = self._commit_split(project_id, list(segments), base_revision)
        announce_revision(RevisionChange("split", project_id, revision, self))
        return revision

    def update_split(
        self,
        project_id: str,
        change: Callable[[list[SavedSegment] | None], list[SavedSegment]],
        base_revision: int | None = None,
    ) -> Versioned[list[SavedSegment]]:
        with self._lock:
            found = self._splits.get(project_id)
            current = 0 if found is None else found[1]
            check_base_revision("split project", project_id, current, base_revision)
            segments = list(change(None if found is None else list(found[0])))
            revision = self._commit_split(project_id, segments, None)
        announce_revision(RevisionChange("split", project_id, revision, self))
        return Versioned(segments, revision)

    def _commit_split(self, project_id: str, segments: list[SavedSegment], base_revision: int | None) -> int:
        found = self._splits.get(project_id)
        current = 0 if found is None else found[1]
        check_base_revision("split project", project_id, current, base_revision)
        self._splits[project_id] = (segments, current + 1)
        return current + 1

    # --- timeline projects --------------------------------------------------------

    def list_timelines(self) -> list[str]:
        with self._lock:
            return sorted(self._timelines)

    def timeline_exists(self, name: str) -> bool:
        timeline.validate_name(name)
        with self._lock:
            return name in self._timelines

    def load_timeline(self, name: str) -> Project:
        timeline.validate_name(name)
        with self._lock:
            found = self._timelines.get(name)
        if found is None:
            return timeline.empty_project()
        return replace(copy.deepcopy(found[0]), revision=found[1])

    def load_existing_timeline(self, name: str) -> Project:
        if not self.timeline_exists(name):
            raise ProjectNotFoundError(f"no project named {name!r}")
        return self.load_timeline(name)

    def save_timeline(self, name: str, project: Project, base_revision: int | None = None) -> int:
        timeline.validate_name(name)
        with self._lock:
            revision = self._commit_timeline(name, project, base_revision)
        announce_revision(RevisionChange("timeline", name, revision, self))
        return revision

    def update_timeline(
        self, name: str, change: Callable[[Project], Project], base_revision: int | None = None
    ) -> Project:
        timeline.validate_name(name)
        with self._lock:
            found = self._timelines.get(name)
            if found is None:
                raise ProjectNotFoundError(f"no project named {name!r}")
            check_base_revision("timeline project", name, found[1], base_revision)
            new = change(replace(copy.deepcopy(found[0]), revision=found[1]))
            revision = self._commit_timeline(name, new, None)
        announce_revision(RevisionChange("timeline", name, revision, self))
        return replace(new, revision=revision)

    def _commit_timeline(self, name: str, project: Project, base_revision: int | None) -> int:
        found = self._timelines.get(name)
        current = 0 if found is None else found[1]
        check_base_revision("timeline project", name, current, base_revision)
        self._timelines[name] = (project, current + 1)
        return current + 1

    def create_timeline(self, name: str) -> Project:
        timeline.validate_name(name)
        with self._lock:
            if name in self._timelines:
                raise ProjectExistsError(f"project {name!r} already exists")
            project = timeline.empty_project()
            self._timelines[name] = (project, 0)
        announce_revision(RevisionChange("timeline", name, 0, self))
        return project

    def peek_revisions(self, split_ids: Iterable[str] = ()) -> dict[tuple[str, str], int]:
        with self._lock:
            found: dict[tuple[str, str], int] = {("timeline", n): rev for n, (_, rev) in self._timelines.items()}
            for project_id in split_ids:
                split = self._splits.get(project_id)
                found[("split", project_id)] = 0 if split is None else split[1]
        return found
