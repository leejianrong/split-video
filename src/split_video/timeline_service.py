"""Headless operations on named timeline projects.

The glue between `timeline_ops` (pure editing rules) and a `ProjectStore`: it
loads a project, applies one change, saves it, and probes sources where the
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
from typing import Any

from split_video import timeline, timeline_ops
from split_video.ffmpeg import MediaInfo, ProbeError
from split_video.media_store import MediaError, MediaOutsideRootError, MediaStore
from split_video.render import render
from split_video.store import ProjectStore
from split_video.timeline import (
    OutputExistsError,
    OutsideRootError,
    Project,
    SourceNotFoundError,
    TimelineError,
)

_lock = threading.Lock()


def source_duration(media: MediaStore, source: str) -> float | None:
    """Duration of a clip's source file, or None if it's missing or unreadable."""
    if not source:  # text clips have no source
        return None
    try:
        return media.probe(timeline.source_media_id(media, source)).duration
    except (TimelineError, MediaError, OSError, ProbeError):
        return None


def _probe_new_source(media: MediaStore, rel: str) -> tuple[str, MediaInfo]:
    """A canonical source id and its media info, for a path the caller named."""
    try:
        media_id = media.resolve(rel)
    except MediaOutsideRootError:
        raise OutsideRootError(f"{rel!r} is outside the project directory") from None
    if not media.is_file(media_id):
        raise SourceNotFoundError(f"file not found: {rel}")
    try:
        return media_id, media.probe(media_id)
    except ProbeError as exc:
        raise TimelineError(str(exc)) from None


# --- loading / saving -------------------------------------------------------


def load_existing(store: ProjectStore, name: str) -> Project:
    """The project `name`; `ProjectNotFoundError` if it doesn't exist."""
    return store.load_existing_timeline(name)


def create_project(store: ProjectStore, name: str) -> Project:
    with _lock:
        return store.create_timeline(name)


def edit(store: ProjectStore, name: str, change: Callable[[Project], Project]) -> Project:
    """Load `name`, apply `change`, save and return the result."""
    with _lock:
        project = change(load_existing(store, name))
        store.save_timeline(name, project)
    return project


# --- operations -------------------------------------------------------------


def video_track(project: Project) -> timeline.Track:
    track = next((t for t in project.tracks if t.kind == "video"), None)
    if track is None:
        raise TimelineError("the project has no video track")
    return track


def track_at(project: Project, position: int | None) -> timeline.Track:
    """The track at 1-based `position` in the project's track list, or the
    main video track when `position` is None."""
    if position is None:
        return video_track(project)
    if not 1 <= position <= len(project.tracks):
        raise TimelineError(f"no track {position}; the project has {len(project.tracks)}")
    return project.tracks[position - 1]


def clip_id_at(project: Project, position: int, track: int | None = None) -> str:
    """The id of the clip at 1-based `position` on a track (default: the main video track)."""
    clips = track_at(project, track).clips
    if not 1 <= position <= len(clips):
        raise TimelineError(f"no clip {position}; the track has {len(clips)}")
    return clips[position - 1].id


def add_clip(
    store: ProjectStore,
    media: MediaStore,
    name: str,
    path: str,
    *,
    track_id: str | None = None,
    index: int | None = None,
    start: float | None = None,
    in_point: float = 0.0,
    out_point: float | None = None,
) -> Project:
    source, info = _probe_new_source(media, path)

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

    return edit(store, name, change)


def trim_clip(
    store: ProjectStore,
    media: MediaStore,
    name: str,
    clip_id: str,
    *,
    in_point: float | None = None,
    out_point: float | None = None,
) -> Project:
    def change(project: Project) -> Project:
        track, i = timeline_ops.find_clip(project, clip_id)
        duration = float("inf") if track.clips[i].kind == "text" else source_duration(media, track.clips[i].source)
        if duration is None:
            raise TimelineError("the clip's source file is missing or unreadable")
        return timeline_ops.trim_clip(project, clip_id, duration, in_point=in_point, out_point=out_point)

    return edit(store, name, change)


def add_text_clip(store: ProjectStore, name: str, track_id: str, text: str, duration: float, **options: Any) -> Project:
    def change(project: Project) -> Project:
        new, _ = timeline_ops.add_text_clip(project, track_id, text, duration, **options)
        return new

    return edit(store, name, change)


def set_clip_properties(store: ProjectStore, name: str, clip_id: str, **properties: float | bool) -> Project:
    return edit(store, name, lambda p: timeline_ops.set_clip_properties(p, clip_id, **properties))


def set_text_properties(store: ProjectStore, name: str, clip_id: str, **properties: str | float | None) -> Project:
    return edit(store, name, lambda p: timeline_ops.set_text_properties(p, clip_id, **properties))


def add_track(store: ProjectStore, name: str, kind: str) -> Project:
    def change(project: Project) -> Project:
        new, _ = timeline_ops.add_track(project, kind)
        return new

    return edit(store, name, change)


def move_track(store: ProjectStore, name: str, track_id: str, to_index: int) -> Project:
    return edit(store, name, lambda p: timeline_ops.move_track(p, track_id, to_index))


def remove_track(store: ProjectStore, name: str, track_id: str) -> Project:
    return edit(store, name, lambda p: timeline_ops.remove_track(p, track_id))


def place_clip(
    store: ProjectStore, name: str, clip_id: str, *, start: float | None = None, track_id: str | None = None
) -> Project:
    return edit(store, name, lambda p: timeline_ops.place_clip(p, clip_id, start=start, track_id=track_id))


def move_clip(store: ProjectStore, name: str, clip_id: str, to_index: int) -> Project:
    return edit(store, name, lambda p: timeline_ops.move_clip(p, clip_id, to_index))


def remove_clip(store: ProjectStore, name: str, clip_id: str) -> Project:
    return edit(store, name, lambda p: timeline_ops.remove_clip(p, clip_id))


def set_transition(store: ProjectStore, name: str, clip_id: str, transition: str, duration: float = 0.0) -> Project:
    return edit(store, name, lambda p: timeline_ops.set_transition(p, clip_id, transition, duration))


# --- rendering ---------------------------------------------------------------


def plan_render(
    store: ProjectStore, media: MediaStore, name: str, output: str | None, overwrite: bool
) -> tuple[Project, str, str]:
    """Check a render request and return the project, output filename and the
    output's media id (a plain filename is a file in the root)."""
    project = load_existing(store, name)
    filename = output or f"{name}-render.mp4"
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise TimelineError("output must be a plain filename")
    if not filename.lower().endswith(".mp4"):
        raise TimelineError("output must end in .mp4")
    if media.exists(filename) and not overwrite:
        raise OutputExistsError(f"{filename} already exists")
    if not any(t.clips for t in project.tracks):
        raise TimelineError("the timeline has no clips to render")
    return project, filename, filename


def run_render(
    project: Project, media: MediaStore, out_id: str, progress: Callable[[float], None] | None = None
) -> None:
    render(project, media, out_id, progress)


# --- describing ---------------------------------------------------------------


def describe(media: MediaStore, name: str, project: Project) -> dict[str, Any]:
    """JSON-friendly view of a project, with clips numbered from 1 per track."""
    return {
        "name": name,
        "duration": project.duration,
        "tracks": [
            {
                "index": n + 1,
                "id": t.id,
                "kind": t.kind,
                "magnetic": t.magnetic,
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
                        "source_duration": source_duration(media, c.source),
                        "gain": c.gain,
                        "muted": c.muted,
                        "transition": c.transition,
                        "transition_duration": c.transition_duration,
                        "kind": c.kind,
                        "text": c.text,
                        "font_size": c.font_size,
                        "color": c.color,
                        "x": c.x,
                        "y": c.y,
                        "scale": c.scale,
                        "opacity": c.opacity,
                        "brightness": c.brightness,
                        "contrast": c.contrast,
                        "saturation": c.saturation,
                    }
                    for i, c in enumerate(t.clips)
                ],
            }
            for n, t in enumerate(project.tracks)
        ],
    }
