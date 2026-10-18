"""The multi-clip timeline project model and its file format.

Separate from `project.py`/`store.py`, which model one video cut
into segments: a timeline project arranges clips from several source videos
on tracks and is saved under its own name by a `ProjectStore` (locally a
`<name>.split-video-timeline.json` file in the directory being edited). Sources are stored as paths relative to that
directory (the editor's browse root), so the file stays valid when the
folder is moved or mounted at a different path, e.g. into Docker.

The model already has tracks, each holding clips placed at an explicit
`start`, even though the first phase only edits one gap-free video track:
that way later phases (overlays, audio tracks) don't need a new file format.

Nothing here touches ffmpeg or the disk (see `store.py` for persistence, `media_store.py` for media); editing rules
live in `timeline_ops.py` and rendering in `render.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from split_video.media_store import MediaOutsideRootError, MediaStore

FORMAT_VERSION = 1

TRACK_KINDS = ("video", "audio")
TRANSITIONS = ("cut", "crossfade")


class TimelineError(ValueError):
    """The request can't be applied to the timeline (bad path, bad range, ...)."""


@dataclass(frozen=True)
class Clip:
    id: str
    source: str  # media id: the path relative to the project's root directory
    in_point: float  # seconds into the source where the clip starts
    out_point: float  # seconds into the source where the clip ends
    start: float = 0.0  # where the clip begins on the track, in seconds
    gain: float = 1.0
    muted: bool = False
    # How this clip is joined to the one before it on the same track.
    transition: str = "cut"
    transition_duration: float = 0.0
    # Overlay placement for clips on upper video tracks: where the clip's
    # centre sits as a fraction of the frame, its width as a fraction of the
    # frame width (1.0 = full width), and how opaque it is. The main track's
    # clips always fill the frame.
    x: float = 0.5
    y: float = 0.5
    scale: float = 1.0
    opacity: float = 1.0
    # Colour adjustment, applied to the clip's video (ffmpeg `eq` semantics:
    # brightness -1..1 around 0, contrast and saturation multipliers around 1).
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    # A text clip ("text" kind) has no source file: it draws `text` for its
    # duration, `font_size` as a fraction of the frame height, in `color`.
    kind: str = "media"
    text: str = ""
    font_size: float = 0.08
    color: str = "#ffffff"

    @property
    def duration(self) -> float:
        return self.out_point - self.in_point

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass(frozen=True)
class Track:
    id: str
    kind: str = "video"
    clips: tuple[Clip, ...] = ()
    # A magnetic track keeps its clips back to back in list order (the main
    # video track). Other tracks place clips at any start time; clips on one
    # track may not overlap. Files from before this field load as magnetic.
    magnetic: bool = True

    @property
    def end(self) -> float:
        return max((c.end for c in self.clips), default=0.0)


@dataclass(frozen=True)
class Project:
    tracks: tuple[Track, ...] = field(default_factory=tuple)

    @property
    def duration(self) -> float:
        return max((t.end for t in self.tracks), default=0.0)


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def empty_project() -> Project:
    """A project with the one video track Phase 2 edits."""
    return Project(tracks=(Track(id=new_id(), kind="video"),))


def source_media_id(media: MediaStore, source: str) -> str:
    """The canonical media id of a clip's `source`, as stored in a project file.

    Raises `OutsideRootError` if it would leave the store, so a crafted project
    file or API call can't make the render read arbitrary files.
    """
    try:
        return media.resolve(source, strict=True)
    except MediaOutsideRootError:
        raise OutsideRootError(f"source {source!r} is outside the project directory") from None


def validate_name(name: str) -> None:
    """Raise `TimelineError` unless `name` is usable as a timeline project name."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise TimelineError(f"invalid project name {name!r}")


# --- serialisation ----------------------------------------------------------


def to_dict(project: Project) -> dict[str, Any]:
    return {
        "version": FORMAT_VERSION,
        "tracks": [
            {
                "id": t.id,
                "kind": t.kind,
                "magnetic": t.magnetic,
                "clips": [
                    {
                        "id": c.id,
                        "source": c.source,
                        "in": c.in_point,
                        "out": c.out_point,
                        "start": c.start,
                        "gain": c.gain,
                        "muted": c.muted,
                        "transition": c.transition,
                        "transition_duration": c.transition_duration,
                        "x": c.x,
                        "y": c.y,
                        "scale": c.scale,
                        "opacity": c.opacity,
                        "brightness": c.brightness,
                        "contrast": c.contrast,
                        "saturation": c.saturation,
                        "kind": c.kind,
                        "text": c.text,
                        "font_size": c.font_size,
                        "color": c.color,
                    }
                    for c in t.clips
                ],
            }
            for t in project.tracks
        ],
    }


def from_dict(data: dict[str, Any]) -> Project:
    tracks = []
    for t in data["tracks"]:
        kind = str(t.get("kind", "video"))
        if kind not in TRACK_KINDS:
            raise ValueError(f"unknown track kind {kind!r}")
        clips = tuple(
            Clip(
                id=str(c["id"]),
                source=str(c["source"]),
                in_point=float(c["in"]),
                out_point=float(c["out"]),
                start=float(c.get("start", 0.0)),
                gain=float(c.get("gain", 1.0)),
                muted=bool(c.get("muted", False)),
                transition=str(c.get("transition", "cut")),
                transition_duration=float(c.get("transition_duration", 0.0)),
                x=float(c.get("x", 0.5)),
                y=float(c.get("y", 0.5)),
                scale=float(c.get("scale", 1.0)),
                opacity=float(c.get("opacity", 1.0)),
                brightness=float(c.get("brightness", 0.0)),
                contrast=float(c.get("contrast", 1.0)),
                saturation=float(c.get("saturation", 1.0)),
                kind=str(c.get("kind", "media")),
                text=str(c.get("text", "")),
                font_size=float(c.get("font_size", 0.08)),
                color=str(c.get("color", "#ffffff")),
            )
            for c in t.get("clips", [])
        )
        tracks.append(Track(id=str(t["id"]), kind=kind, clips=clips, magnetic=bool(t.get("magnetic", True))))
    return Project(tracks=tuple(tracks))


class ProjectNotFoundError(TimelineError):
    """No timeline project with that name exists in the directory."""


class ProjectExistsError(TimelineError):
    """A timeline project with that name already exists."""


class OutsideRootError(TimelineError):
    """A path resolves outside the project directory."""


class SourceNotFoundError(TimelineError):
    """A clip's source file doesn't exist."""


class OutputExistsError(TimelineError):
    """The render output exists and overwriting wasn't asked for."""
