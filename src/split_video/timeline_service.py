"""Headless operations on named timeline projects in a directory.

The glue between `timeline_ops` (pure editing rules) and the disk: it loads
a project file, applies one change, saves it, and probes sources where the
rules need their duration. The editor's `/api/compose/*` endpoints, the
`compose` CLI subcommands and the MCP tools all call these functions, so the
three front doors can't drift apart. Failures are `TimelineError` subclasses
(see `timeline.py`); each caller maps them to its own error style.

Clips on the video track are addressed by 1-based position in the CLI and MCP
(`clip_id_at`), like segment numbers in the split editor, and by id in the API.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from split_video import timeline, timeline_ops
from split_video.ffmpeg import MediaInfo, ProbeError, probe_media
from split_video.render import render
from split_video.timeline import (
    OutputExistsError,
    OutsideRootError,
    Project,
    ProjectExistsError,
    ProjectNotFoundError,
    SourceNotFoundError,
    TimelineError,
)

_lock = threading.Lock()
_duration_cache: dict[tuple[str, int], float] = {}


def source_duration(root: Path, source: str) -> float | None:
    """Duration of a clip's source file, or None if it's missing or unreadable."""
    if not source:  # text clips have no source
        return None
    try:
        path = timeline.resolve_source(root, source)
        key = (str(path), path.stat().st_mtime_ns)
        if key not in _duration_cache:
            _duration_cache[key] = probe_media(path).duration
        return _duration_cache[key]
    except (TimelineError, OSError, ProbeError):
        return None


def _probe_new_source(root: Path, rel: str) -> tuple[str, MediaInfo]:
    """A root-relative source string and its media info, for a path the caller named."""
    root = root.resolve()
    path = (root / rel.lstrip("/")).resolve()
    if path != root and root not in path.parents:
        raise OutsideRootError(f"{rel!r} is outside the project directory")
    if not path.is_file():
        raise SourceNotFoundError(f"file not found: {rel}")
    try:
        return timeline.relative_source(root, path), probe_media(path)
    except ProbeError as exc:
        raise TimelineError(str(exc)) from None


# --- loading / saving -------------------------------------------------------


def load_existing(root: Path, name: str) -> tuple[Path, Project]:
    path = timeline.project_path(root, name)
    if not path.exists():
        raise ProjectNotFoundError(f"no project named {name!r}")
    return path, timeline.load(path)


def create_project(root: Path, name: str) -> Project:
    path = timeline.project_path(root, name)
    with _lock:
        if path.exists():
            raise ProjectExistsError(f"project {name!r} already exists")
        project = timeline.empty_project()
        timeline.save(path, project)
    return project


def edit(root: Path, name: str, change: Callable[[Project], Project]) -> Project:
    """Load `name`, apply `change`, save and return the result."""
    with _lock:
        path, project = load_existing(root, name)
        project = change(project)
        timeline.save(path, project)
    return project


# --- operations -------------------------------------------------------------


def video_track(project: Project) -> timeline.Track:
    track = next((t for t in project.tracks if t.kind == "video"), None)
    if track is None:
        raise TimelineError("the project has no video track")
    return track


def clip_id_at(project: Project, position: int) -> str:
    """The id of the clip at 1-based `position` on the video track."""
    clips = video_track(project).clips
    if not 1 <= position <= len(clips):
        raise TimelineError(f"no clip {position}; the video track has {len(clips)}")
    return clips[position - 1].id


def add_clip(
    root: Path,
    name: str,
    path: str,
    *,
    track_id: str | None = None,
    index: int | None = None,
    start: float | None = None,
    in_point: float = 0.0,
    out_point: float | None = None,
) -> Project:
    source, info = _probe_new_source(root, path)

    def change(project: Project) -> Project:
        target = timeline_ops.find_track(project, track_id) if track_id else video_track(project)
        if target.kind == "video" and info.width is None:
            raise TimelineError(f"{path} has no video stream, so it can't go on a video track")
        if target.kind == "audio" and not info.has_audio:
            raise TimelineError(f"{path} has no audio stream, so it can't go on an audio track")
        new, _ = timeline_ops.add_clip(
            project,
            target.id,
            source,
            info.duration,
            in_point=in_point,
            out_point=out_point,
            index=index,
            start=start,
        )
        return new

    return edit(root, name, change)


def trim_clip(
    root: Path, name: str, clip_id: str, *, in_point: float | None = None, out_point: float | None = None
) -> Project:
    def change(project: Project) -> Project:
        track, i = timeline_ops.find_clip(project, clip_id)
        duration = float("inf") if track.clips[i].kind == "text" else source_duration(root, track.clips[i].source)
        if duration is None:
            raise TimelineError("the clip's source file is missing or unreadable")
        return timeline_ops.trim_clip(project, clip_id, duration, in_point=in_point, out_point=out_point)

    return edit(root, name, change)


def add_text_clip(root: Path, name: str, track_id: str, text: str, duration: float, **options: Any) -> Project:
    def change(project: Project) -> Project:
        new, _ = timeline_ops.add_text_clip(project, track_id, text, duration, **options)
        return new

    return edit(root, name, change)


def set_clip_properties(root: Path, name: str, clip_id: str, **properties: float | bool) -> Project:
    return edit(root, name, lambda p: timeline_ops.set_clip_properties(p, clip_id, **properties))


def set_text_properties(root: Path, name: str, clip_id: str, **properties: str | float | None) -> Project:
    return edit(root, name, lambda p: timeline_ops.set_text_properties(p, clip_id, **properties))


def add_track(root: Path, name: str, kind: str) -> Project:
    def change(project: Project) -> Project:
        new, _ = timeline_ops.add_track(project, kind)
        return new

    return edit(root, name, change)


def remove_track(root: Path, name: str, track_id: str) -> Project:
    return edit(root, name, lambda p: timeline_ops.remove_track(p, track_id))


def place_clip(
    root: Path, name: str, clip_id: str, *, start: float | None = None, track_id: str | None = None
) -> Project:
    return edit(root, name, lambda p: timeline_ops.place_clip(p, clip_id, start=start, track_id=track_id))


def move_clip(root: Path, name: str, clip_id: str, to_index: int) -> Project:
    return edit(root, name, lambda p: timeline_ops.move_clip(p, clip_id, to_index))


def remove_clip(root: Path, name: str, clip_id: str) -> Project:
    return edit(root, name, lambda p: timeline_ops.remove_clip(p, clip_id))


def set_transition(root: Path, name: str, clip_id: str, transition: str, duration: float = 0.0) -> Project:
    return edit(root, name, lambda p: timeline_ops.set_transition(p, clip_id, transition, duration))


# --- rendering ---------------------------------------------------------------


def plan_render(root: Path, name: str, output: str | None, overwrite: bool) -> tuple[Project, str, Path]:
    """Check a render request and return the project, output filename and path."""
    _, project = load_existing(root, name)
    filename = output or f"{name}-render.mp4"
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise TimelineError("output must be a plain filename")
    if not filename.lower().endswith(".mp4"):
        raise TimelineError("output must end in .mp4")
    out_path = root / filename
    if out_path.exists() and not overwrite:
        raise OutputExistsError(f"{filename} already exists")
    if not any(t.clips for t in project.tracks):
        raise TimelineError("the timeline has no clips to render")
    return project, filename, out_path


def run_render(project: Project, root: Path, out_path: Path, progress: Callable[[float], None] | None = None) -> None:
    render(project, root, out_path, progress)


# --- describing ---------------------------------------------------------------


def describe(root: Path, name: str, project: Project) -> dict[str, Any]:
    """JSON-friendly view of a project, with clips numbered from 1 per track."""
    return {
        "name": name,
        "duration": project.duration,
        "tracks": [
            {
                "id": t.id,
                "kind": t.kind,
                "clips": [
                    {
                        "index": i + 1,
                        "id": c.id,
                        "source": c.source,
                        "in": c.in_point,
                        "out": c.out_point,
                        "start": c.start,
                        "end": c.end,
                        "duration": c.duration,
                        "source_duration": source_duration(root, c.source),
                        "gain": c.gain,
                        "muted": c.muted,
                        "transition": c.transition,
                        "transition_duration": c.transition_duration,
                    }
                    for i, c in enumerate(t.clips)
                ],
            }
            for t in project.tracks
        ],
    }
