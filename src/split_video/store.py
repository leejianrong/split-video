"""Where projects live: the `ProjectStore` interface and an in-memory version.

The engine (`project.py`, `timeline_service.py`, the editor routes, CLI and
MCP server) only ever reads and writes projects through a `ProjectStore`, so
it never builds a project file path itself. `local_store.LocalProjectStore`
keeps today's on-disk layout (a sidecar next to each video for split
projects, `<name>.split-video-timeline.json` files in the workspace root for
timeline projects); a hosted deployment would supply a database-backed
implementation of the same interface. `InMemoryProjectStore` is the
reference implementation and what the unit tests run the engine against.

Source media, the derived caches and file-path handling are deliberately not
part of this interface (see the MediaStore card).
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from split_video import timeline
from split_video.timeline import Project, ProjectExistsError, ProjectNotFoundError


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


@runtime_checkable
class ProjectStore(Protocol):
    """Load and save split-point projects and timeline projects by id."""

    # --- split-point projects, keyed by the video's root-relative path --------

    def load_split(self, project_id: str) -> list[SavedSegment] | None:
        """The saved segments for the video `project_id`, or None if there
        aren't any or they can't be read. Never raises for a missing or
        corrupt project: that falls back to fresh detection."""
        ...

    def save_split(self, project_id: str, segments: list[SavedSegment]) -> None:
        """Replace the saved segments for `project_id`. Atomic: a concurrent
        `load_split` sees the old or the new list, never a partial one."""
        ...

    # --- timeline projects, keyed by name -------------------------------------

    def list_timelines(self) -> list[str]:
        """Names of all timeline projects, sorted."""
        ...

    def timeline_exists(self, name: str) -> bool: ...

    def load_timeline(self, name: str) -> Project:
        """The project `name`; an empty project if it is missing or damaged
        (use `timeline_exists` or `load_existing_timeline` to tell)."""
        ...

    def load_existing_timeline(self, name: str) -> Project:
        """Like `load_timeline`, but raises `ProjectNotFoundError` if `name`
        doesn't exist."""
        ...

    def save_timeline(self, name: str, project: Project) -> None:
        """Replace timeline project `name`, atomically."""
        ...

    def create_timeline(self, name: str) -> Project:
        """Create an empty timeline project; `ProjectExistsError` if `name`
        is taken. Callers serialise this with `timeline_service`'s lock."""
        ...


class InMemoryProjectStore:
    """A `ProjectStore` that keeps everything in dicts; nothing touches disk."""

    def __init__(self) -> None:
        self._splits: dict[str, list[SavedSegment]] = {}
        self._timelines: dict[str, Project] = {}
        self._lock = threading.Lock()

    def load_split(self, project_id: str) -> list[SavedSegment] | None:
        with self._lock:
            saved = self._splits.get(project_id)
            return None if saved is None else list(saved)

    def save_split(self, project_id: str, segments: list[SavedSegment]) -> None:
        with self._lock:
            self._splits[project_id] = list(segments)

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
        return timeline.empty_project() if found is None else copy.deepcopy(found)

    def load_existing_timeline(self, name: str) -> Project:
        if not self.timeline_exists(name):
            raise ProjectNotFoundError(f"no project named {name!r}")
        return self.load_timeline(name)

    def save_timeline(self, name: str, project: Project) -> None:
        timeline.validate_name(name)
        with self._lock:
            self._timelines[name] = project

    def create_timeline(self, name: str) -> Project:
        timeline.validate_name(name)
        with self._lock:
            if name in self._timelines:
                raise ProjectExistsError(f"project {name!r} already exists")
            project = timeline.empty_project()
            self._timelines[name] = project
        return project
