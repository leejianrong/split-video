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

# Version 2 (asset ids): media clips carry `asset`, the id of an entry in the
# workspace's asset table (`assets.py`), next to `source`, which is now only a
# hint of the file's last known root-relative path. Version 1 files (`source`
# only) still load; their clips come back with an empty `asset` and are linked
# by `assets.link_project`, which the service layer runs on load and before
# every edit, so the next save writes version 2.
#
# Clip crop (SPL-51) is an additive optional field, so the version stays 2: a clip's `crop`
# object `{"x","y","w","h"}` (fractions of the source frame, top-left origin) is written only
# when it is not the full frame, and a file without it loads with the full-frame default
# (an older build ignores the key it doesn't know). No migration is needed in either direction.
#
# Split metadata (SPL-116) is the same kind of change: a clip's optional `label`, `label_color`, `included`
# (written only when false) and `export_name` hold what a split project's segment had. The version stays 2.
FORMAT_VERSION = 2

TRACK_KINDS = ("video", "audio")
TRANSITIONS = ("cut", "crossfade")


class TimelineError(ValueError):
    """The request can't be applied to the timeline (bad path, bad range, ...)."""


@dataclass(frozen=True)
class Clip:
    id: str
    # Last known location of the file: its media id, the path relative to the
    # project's root directory. A hint: `asset` is the reference, and the asset
    # table says where the file is now.
    source: str
    in_point: float  # seconds into the source where the clip starts
    out_point: float  # seconds into the source where the clip ends
    # The asset id (`a_` + 16 hex digits, see `assets.py`) of a media clip's
    # file; "" for text clips and for clips from a version 1 file not yet linked.
    asset: str = ""
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
    # Crop: the part of the source frame shown, as fractions of the source frame with the origin at
    # its top-left. The default is the full frame (no crop). Validated by `timeline_ops.check_crop`.
    crop_x: float = 0.0
    crop_y: float = 0.0
    crop_w: float = 1.0
    crop_h: float = 1.0
    # A text clip ("text" kind) has no source file: it draws `text` for its
    # duration, `font_size` as a fraction of the frame height, in `color`.
    kind: str = "media"
    text: str = ""
    font_size: float = 0.08
    color: str = "#ffffff"
    # Split metadata (SPL-116): a clip cut from a recording by the split tools is one segment. All four are
    # optional and written only when set: `label` is its display text, `label_color` its band colour
    # (`#rrggbb`; `color` above is a text clip's), `included` whether "each segment as its own file" writes it,
    # `export_name` the output file name without extension (None: the numbered default).
    label: str = ""
    label_color: str | None = None
    included: bool = True
    export_name: str | None = None

    @property
    def cropped(self) -> bool:
        return (self.crop_x, self.crop_y, self.crop_w, self.crop_h) != (0.0, 0.0, 1.0, 1.0)

    @property
    def crop(self) -> dict[str, float] | None:
        """The crop as `{"x","y","w","h"}`, or None for the full frame."""
        if not self.cropped:
            return None
        return {"x": self.crop_x, "y": self.crop_y, "w": self.crop_w, "h": self.crop_h}

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
    # The stored revision this value was loaded at or saved as (0 for a project never saved). Stamped
    # by the `ProjectStore`, which owns it; editing functions build new `Project`s without it and the
    # store ignores whatever they carry. Not part of equality: two projects with the same tracks are equal.
    revision: int = field(default=0, compare=False)

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


def ms(seconds: float) -> float:
    """`seconds` rounded to a millisecond, for OUTPUT only (sums of floats show as 123.36000000000013 otherwise).
    What is stored in the timeline file is never rounded, so `plan_hash`es and saved files are unchanged."""
    return round(seconds, 3) + 0.0  # + 0.0: no "-0.0"


def shown_crop(crop: dict[str, float] | None) -> dict[str, float] | None:
    """A crop box for output: fractions of the frame to 6 digits (finer than a pixel of any real source)."""
    return None if crop is None else {k: round(v, 6) + 0.0 for k, v in crop.items()}


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
                        # Text clips have no file; an unlinked media clip is one with a source but no asset.
                        **({"asset": c.asset} if c.asset else {}),
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
                        **({"crop": c.crop} if c.cropped else {}),
                        "kind": c.kind,
                        "text": c.text,
                        "font_size": c.font_size,
                        "color": c.color,
                        **({"label": c.label} if c.label else {}),
                        **({"label_color": c.label_color} if c.label_color is not None else {}),
                        **({} if c.included else {"included": False}),
                        **({"export_name": c.export_name} if c.export_name is not None else {}),
                    }
                    for c in t.clips
                ],
            }
            for t in project.tracks
        ],
    }


def _crop_from_dict(raw: Any) -> dict[str, float]:
    """The `crop_*` fields of a clip from its optional `crop` object (full frame when absent)."""
    if not isinstance(raw, dict):
        return {}
    return {
        "crop_x": float(raw.get("x", 0.0)),
        "crop_y": float(raw.get("y", 0.0)),
        "crop_w": float(raw.get("w", 1.0)),
        "crop_h": float(raw.get("h", 1.0)),
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
                asset=str(c.get("asset", "")),
                source=str(c.get("source", "")),
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
                **_crop_from_dict(c.get("crop")),
                kind=str(c.get("kind", "media")),
                text=str(c.get("text", "")),
                font_size=float(c.get("font_size", 0.08)),
                color=str(c.get("color", "#ffffff")),
                label=str(c.get("label", "")),
                label_color=None if c.get("label_color") is None else str(c["label_color"]),
                included=bool(c.get("included", True)),
                export_name=None if c.get("export_name") is None else str(c["export_name"]),
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


class AssetNotFoundError(TimelineError):
    """No asset with that id is in the workspace's asset table."""


class OutputExistsError(TimelineError):
    """The render output exists and overwriting wasn't asked for."""
