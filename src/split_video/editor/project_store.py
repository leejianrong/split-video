"""Sidecar disk persistence for a user's in-progress split points.

Deliberately separate from `cache_store.py`: that module holds *derived*
analysis results (silence scans, waveform peaks, classification) that are
safe to lose and cheaply regenerate. This module holds the split points a
user placed, dragged, or deleted by hand — the whole reason #14 exists — so
it's a plain, visible file next to the video rather than tucked into the
hidden `.split-video-cache/` directory: it's the user's own edit, not a
cache of one.

Same constraint as the cache, though (see cache_store's docstring): this
has to be a sidecar next to the source video, not a centralized path,
since `make dev` only bind-mounts the video's own directory into the
(`--rm`'d) container.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

FORMAT_VERSION = 2


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


def _project_path(source: Path) -> Path:
    return source.parent / f"{source.name}.split-video-project.json"


def load(source: Path) -> list[SavedSegment] | None:
    """The saved segment list for `source`, or None if there isn't one (or
    it can't be read) — never raises, since a missing/corrupt project file
    should fall back to fresh detection, not fail opening the video."""
    try:
        with _project_path(source).open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
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


def save(source: Path, segments: list[SavedSegment]) -> None:
    """Persist `segments`, replacing whatever was saved before.

    Written to a temp file and renamed into place so a concurrent `load`
    (e.g. a second `edit` process pointed at the same file) never observes
    a partially-written project file.
    """
    path = _project_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": FORMAT_VERSION,
        "source_file": source.name,
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
    tmp_path = path.parent / f"{path.name}.tmp"
    with tmp_path.open("w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp_path, path)
