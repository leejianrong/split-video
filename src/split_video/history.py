"""Undo and redo: a linear edit history kept next to each project (SPL-75).

Undo is a forward write. It saves the previous state as a NEW revision, so revisions stay monotonic and every
client, the events stream, stills and playback caches keep working unchanged. The stores (`ProjectStore.history`,
`ProjectStore.step_history`) keep one log per project and call the pure functions here under the project's write lock;
a log is plain JSON so a store can park it wherever it keeps sidecars:

    {"cursor": 2, "states": [{"label": None, "revision": 4, "project": {...}},      # the state before the first edit
                             {"label": "Split clip", "revision": 5, "project": {...}},   # the state each edit produced
                             {"label": "Trim clip", "revision": 8, "project": {...}}]}

`cursor` is the state the project is at; states after it are the redo stack. Every write records one entry (a batch of
ops is one), and a new edit drops the redo stack (no branching). Entries older than `MAX_ENTRIES`, or beyond
`MAX_BYTES` of JSON, are dropped from the front.

Other writers (the CLI, MCP, another tab) go through the same store, so their edits are entries too and undo reverses
the latest write whoever made it; pass `base_revision` to refuse when someone else has moved on. A project whose head
revision is not the one the log ends on (its file was changed by something that does not record history) has no
usable history: the log is restarted at the next write and, until then, nothing can be undone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from split_video.timeline import TimelineError

MAX_ENTRIES = 100
MAX_BYTES = 4 * 1024 * 1024

Step = Literal["undo", "redo"]
Log = dict[str, Any]


class HistoryEmptyError(TimelineError):
    """There is nothing to undo or redo."""

    error_code = "history.empty"

    def __init__(self, step: Step) -> None:
        self.details = {"step": step}
        super().__init__(f"nothing to {step}")


@dataclass(frozen=True)
class HistoryState:
    """What every surface shows of a project's history."""

    can_undo: bool = False
    can_redo: bool = False
    undo_label: str | None = None  # what the next undo reverts, e.g. "Split clip"
    redo_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "can_undo": self.can_undo,
            "can_redo": self.can_redo,
            "undo_label": self.undo_label,
            "redo_label": self.redo_label,
        }


def _usable(log: Log | None, head: int) -> bool:
    return bool(log) and log["states"][log["cursor"]]["revision"] == head


def state_of(log: Log | None, head: int) -> HistoryState:
    """The history at project revision `head`."""
    if not _usable(log, head):
        return HistoryState()
    assert log is not None
    states, cursor = log["states"], log["cursor"]
    has_redo = cursor + 1 < len(states)
    return HistoryState(
        can_undo=cursor > 0,
        can_redo=has_redo,
        undo_label=states[cursor]["label"] if cursor > 0 else None,
        redo_label=states[cursor + 1]["label"] if has_redo else None,
    )


def record(
    log: Log | None, before: dict[str, Any] | None, before_rev: int, after: dict[str, Any], after_rev: int, label: str
) -> Log:
    """The log after a write that took the project from `before` (None: it did not exist) to `after`."""
    if _usable(log, before_rev):
        assert log is not None
        states = log["states"][: log["cursor"] + 1]
    else:
        states = [] if before is None else [{"label": None, "revision": before_rev, "project": before}]
    states = [*states, {"label": label, "revision": after_rev, "project": after}]
    sizes = [len(json.dumps(s["project"], separators=(",", ":"))) for s in states]
    while len(states) > 2 and (len(states) > MAX_ENTRIES + 1 or sum(sizes) > MAX_BYTES):
        del states[0], sizes[0]
    return {"cursor": len(states) - 1, "states": states}


def step(log: Log | None, direction: Step, head: int, new_rev: int) -> tuple[dict[str, Any], Log]:
    """The project dict to save as revision `new_rev` for an undo or redo at head revision `head`, and the moved log.
    `HistoryEmptyError` if there is nothing to step to."""
    shown = state_of(log, head)
    if not (shown.can_undo if direction == "undo" else shown.can_redo):
        raise HistoryEmptyError(direction)
    assert log is not None
    cursor = log["cursor"] + (-1 if direction == "undo" else 1)
    target = log["states"][cursor]
    states = [*log["states"]]
    states[cursor] = {**target, "revision": new_rev}
    return target["project"], {"cursor": cursor, "states": states}
