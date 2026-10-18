"""Errors shared by the engine, the stores and the three front doors.

`ProjectError` lives here (and is re-exported by `project.py`) so that a store can raise
`RevisionConflictError`, which must be catchable both as a split-project failure
(`ProjectError`) and as a timeline failure (`TimelineError`).
"""

from __future__ import annotations

from split_video.timeline import TimelineError


class ProjectError(ValueError):
    """The request can't be applied (bad index, overlapping segments, ...)."""


class RevisionConflictError(ProjectError, TimelineError):
    """A write was based on an older revision of the project than the one now stored.

    `base` is the revision the caller said it was working from, `current` the stored one.
    Nothing was written. Callers reload the project, re-apply their change and retry with
    `base_revision=current`; each surface (HTTP, CLI, MCP) words that for its own users.
    """

    def __init__(self, project: str, current: int, base: int | None, kind: str = "project") -> None:
        self.project = project
        self.current = current
        self.base = base
        self.kind = kind
        super().__init__(
            f"revision conflict on {kind} {project!r}: your change was based on revision {base}, but it is now "
            f"at revision {current}. Reload the project, re-apply your change on top of the latest version, "
            f"and retry with base_revision={current}."
        )
