"""Editing rules for a timeline `Project`.

Like `project.py` for the split editor, this is the one place the rules
live, so the web UI, the CLI and the MCP server can't disagree. Every
function is pure: it takes a `Project` and returns a new one; the caller
saves it. Bad input raises `TimelineError`.

A magnetic track (the main video track) stays gap-free: clips sit back to
back in list order, and a crossfade overlaps a clip with the one before it.
Any other track places clips at an explicit start time and refuses
overlaps. All of that layout lives in `normalize_track`. Tracks are stacked
in list order: a later video track draws over an earlier one.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace

from split_video.timeline import (
    TRACK_KINDS,
    TRANSITIONS,
    Clip,
    Project,
    TimelineError,
    Track,
    new_id,
)

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Shortest clip the editor allows, so a trim can't produce a zero-length clip.
MIN_CLIP = 0.1

# Text size as a fraction of the frame height.
FONT_SIZE_RANGE = (0.01, 0.5)


class RuleViolation(TimelineError):
    """A `TimelineError` that also says which rule it broke, so a caller that reports many
    problems at once (`edit_plan.py`, `edit_ops.py`) can give each a stable code. `reason` is one of
    `in_negative`, `out_past_end`, `too_short`, `crossfade_first`, `crossfade_duration`,
    `crossfade_room`, `start_negative`, `overlap`, `magnetic_start`, `free_index`, `index_range`,
    `move_on_free`, `crossfade_free_track`, `text_target`, `not_text_clip`, `last_video_track`,
    `kind_mismatch`, `main_track_dest`, `crop_invalid`, `crop_not_applicable`, `track_locked`,
    `main_track_hidden`."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# --- lookup -----------------------------------------------------------------


def find_track(project: Project, track_id: str) -> Track:
    for track in project.tracks:
        if track.id == track_id:
            return track
    raise TimelineError(f"no track {track_id!r}")


def find_clip(project: Project, clip_id: str) -> tuple[Track, int]:
    """The track holding `clip_id` and the clip's index on it."""
    for track in project.tracks:
        for i, clip in enumerate(track.clips):
            if clip.id == clip_id:
                return track, i
    raise TimelineError(f"no clip {clip_id!r}")


def check_unlocked(track: Track) -> None:
    """Raise `RuleViolation("track_locked")` if the track is locked (a lane's lock toggle)."""
    if track.locked:
        raise RuleViolation("track_locked", f"track {track.id} is locked; unlock it to edit its clips")


def _replace_track(project: Project, track: Track) -> Project:
    return replace(project, tracks=tuple(track if t.id == track.id else t for t in project.tracks))


# --- layout -----------------------------------------------------------------


def normalize_track(track: Track) -> Track:
    """Make a track's layout legal. A free track is sorted by start and must
    not overlap; a magnetic track is laid out as described below.

    Magnetic: lay clips out back to back and make their transitions consistent.
    The first clip is always a cut. A crossfade is clamped so it never
    outlasts either neighbour, counting the fade a clip already spends on its
    other side. A crossfade of zero length becomes a cut.
    """
    if not track.magnetic:
        return _normalize_free_track(track)
    clips: list[Clip] = []
    prev: Clip | None = None
    for clip in track.clips:
        transition, duration = clip.transition, clip.transition_duration
        if prev is None or transition != "crossfade":
            transition, duration = "cut", 0.0
        else:
            duration = min(duration, clip.duration, prev.duration - prev.transition_duration)
            if duration <= 0:
                transition, duration = "cut", 0.0
        start = 0.0 if prev is None else prev.end - duration
        clip = replace(clip, start=start, transition=transition, transition_duration=duration)
        clips.append(clip)
        prev = clip
    return replace(track, clips=tuple(clips))


def _normalize_free_track(track: Track) -> Track:
    clips = sorted(track.clips, key=lambda c: c.start)
    prev: Clip | None = None
    for clip in clips:
        if clip.start < -1e-9:
            raise RuleViolation("start_negative", "a clip can't start before 0")
        if prev is not None and clip.start < prev.end - 1e-6:
            raise RuleViolation("overlap", f"clip would overlap another clip on the track (until {prev.end:g}s)")
        prev = clip
    # Crossfades are a magnetic-track feature; here every clip is a plain cut.
    clips = [replace(c, transition="cut", transition_duration=0.0) for c in clips]
    return replace(track, clips=tuple(clips))


def _with_clips(project: Project, track: Track, clips: list[Clip]) -> Project:
    """Replace `track`'s clips (every clip edit goes through here, so a locked track refuses all of them)."""
    check_unlocked(track)
    return _replace_track(project, normalize_track(replace(track, clips=tuple(clips))))


# --- operations -------------------------------------------------------------


def add_clip(
    project: Project,
    track_id: str,
    source: str,
    source_duration: float,
    *,
    in_point: float = 0.0,
    out_point: float | None = None,
    index: int | None = None,
    start: float | None = None,
    asset: str = "",
) -> tuple[Project, Clip]:
    """Insert a clip of `source` (which is `source_duration` long) on a track.
    `asset` is the source file's asset id (see `assets.py`); `source` is then its
    last known path, kept as a hint.

    On a magnetic track the clip goes at `index` (default: the end); on a free
    track at `start` seconds (default: after the last clip). Returns the new
    project and the new clip."""
    track = find_track(project, track_id)
    out = source_duration if out_point is None else out_point
    check_range(in_point, out, source_duration)
    clip = Clip(id=new_id(), source=source, in_point=in_point, out_point=out, asset=asset)
    clips = list(track.clips)
    if track.magnetic:
        if start is not None:
            raise RuleViolation("magnetic_start", "this track keeps its clips back to back; use index, not start")
        if index is None:
            index = len(clips)
        if not 0 <= index <= len(clips):
            raise RuleViolation("index_range", f"index {index} out of range (0..{len(clips)})")
        clips.insert(index, clip)
    else:
        if index is not None:
            raise RuleViolation("free_index", "this track places clips by start time; use start, not index")
        clips.append(replace(clip, start=track.end if start is None else start))
    new = _with_clips(project, track, clips)
    return new, find_clip_in(new, clip.id)


def find_clip_in(project: Project, clip_id: str) -> Clip:
    track, i = find_clip(project, clip_id)
    return track.clips[i]


def trim_clip(
    project: Project,
    clip_id: str,
    source_duration: float,
    *,
    in_point: float | None = None,
    out_point: float | None = None,
) -> Project:
    track, i = find_clip(project, clip_id)
    clip = track.clips[i]
    new_in = clip.in_point if in_point is None else in_point
    new_out = clip.out_point if out_point is None else out_point
    check_range(new_in, new_out, source_duration)
    clips = list(track.clips)
    # On a free track, moving the left edge keeps the content where it is in
    # time, so the clip's start follows the in point.
    start = clip.start if track.magnetic else clip.start + (new_in - clip.in_point)
    clips[i] = replace(clip, in_point=new_in, out_point=new_out, start=start)
    return _with_clips(project, track, clips)


def move_clip(project: Project, clip_id: str, to_index: int) -> Project:
    """Move a clip to `to_index` (0-based) among its track's clips."""
    track, i = find_clip(project, clip_id)
    if not track.magnetic:
        raise RuleViolation("move_on_free", "this track places clips by start time; use place_clip")
    clips = list(track.clips)
    if not 0 <= to_index < len(clips):
        raise RuleViolation("index_range", f"index {to_index} out of range (0..{len(clips) - 1})")
    clips.insert(to_index, clips.pop(i))
    return _with_clips(project, track, clips)


def remove_clip(project: Project, clip_id: str) -> Project:
    track, i = find_clip(project, clip_id)
    clips = list(track.clips)
    del clips[i]
    return _with_clips(project, track, clips)


def set_transition(project: Project, clip_id: str, transition: str, duration: float = 0.0) -> Project:
    """Set how `clip_id` joins the clip before it. Unlike the clamping done by
    `normalize_track`, a request that can't be honoured is an error."""
    if transition not in TRANSITIONS:
        raise TimelineError(f"unknown transition {transition!r}; choose from {', '.join(TRANSITIONS)}")
    track, i = find_clip(project, clip_id)
    clip = track.clips[i]
    if not track.magnetic:
        raise RuleViolation("crossfade_free_track", "crossfades are only available on the main track")
    if transition == "cut":
        duration = 0.0
    else:
        prev_room = None if i == 0 else track.clips[i - 1].duration - track.clips[i - 1].transition_duration
        next_fade = track.clips[i + 1].transition_duration if i + 1 < len(track.clips) else 0.0
        check_crossfade(duration, clip.duration, prev_room, next_fade)
    clips = list(track.clips)
    clips[i] = replace(clip, transition=transition, transition_duration=duration)
    return _with_clips(project, track, clips)


def check_crossfade(duration: float, clip_duration: float, prev_room: float | None, next_fade: float) -> None:
    """Raise `RuleViolation` unless a crossfade of `duration` seconds can join a clip (`clip_duration` long,
    whose own next-clip fade is `next_fade`) to the one before it. `prev_room` is how much of that previous
    clip is not already spent on its own fade, or None for the first clip on the track."""
    if prev_room is None:
        raise RuleViolation("crossfade_first", "the first clip on a track has nothing to crossfade from")
    if duration <= 0:
        raise RuleViolation("crossfade_duration", "a crossfade needs a duration above zero")
    room = min(prev_room, clip_duration - next_fade)
    if duration > room + 1e-9:
        raise RuleViolation(
            "crossfade_room",
            f"crossfade of {duration:g}s is longer than the room the neighbouring clips leave ({room:g}s)",
        )


# Settable per-clip properties and their allowed (low, high) range. Everything
# here is a plain number except `muted`.
CLIP_PROPERTY_RANGES: dict[str, tuple[float, float]] = {
    "gain": (0.0, 4.0),
    "x": (0.0, 1.0),
    "y": (0.0, 1.0),
    "scale": (0.05, 4.0),
    "opacity": (0.0, 1.0),
    "brightness": (-1.0, 1.0),
    "contrast": (0.0, 3.0),
    "saturation": (0.0, 3.0),
}


# --- crop (SPL-51) -----------------------------------------------------------

# Smallest width or height of a crop, as a fraction of the source frame: below this the picture is a
# few pixels and the render would be scaling noise.
CROP_MIN = 0.02
_CROP_TOLERANCE = 1e-6  # float rounding allowed when x+w or y+h reaches the frame edge
CROP_ERROR = (
    f"crop must have x and y between 0 and 1, w and h between {CROP_MIN:g} and 1, "
    "and x+w and y+h at most 1 (fractions of the source frame, origin top-left)"
)


def check_crop(x: object, y: object, w: object, h: object) -> dict[str, float]:
    """The one crop validator. Returns the crop as `{"x","y","w","h"}` floats, or raises `RuleViolation`
    (`crop_invalid`) with the stable message `CROP_ERROR`. An overshoot of up to 1e-6 past the frame edge
    (float rounding) is accepted and trimmed to the edge."""
    values = (x, y, w, h)
    if any(isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) for v in values):
        raise RuleViolation("crop_invalid", CROP_ERROR)
    fx, fy, fw, fh = (float(v) for v in values)  # type: ignore[arg-type]
    tol = _CROP_TOLERANCE
    if not (
        -tol <= fx <= 1 and -tol <= fy <= 1 and CROP_MIN - tol <= fw <= 1 + tol and CROP_MIN - tol <= fh <= 1 + tol
    ):
        raise RuleViolation("crop_invalid", CROP_ERROR)
    fx, fy = max(fx, 0.0), max(fy, 0.0)
    fw, fh = max(fw, CROP_MIN), max(fh, CROP_MIN)
    if fx + fw > 1 + tol or fy + fh > 1 + tol:
        raise RuleViolation("crop_invalid", CROP_ERROR)
    return {"x": fx, "y": fy, "w": min(fw, 1 - fx), "h": min(fh, 1 - fy)}


def crop_for_aspect(
    source_w: float,
    source_h: float,
    aspect_w: float,
    aspect_h: float,
    center_x: float = 0.5,
    center_y: float = 0.5,
    zoom: float = 1.0,
) -> dict[str, float]:
    """The largest crop of a `source_w` x `source_h` frame with the aspect ratio `aspect_w`:`aspect_h`
    (for a 9:16 short from a 16:9 recording: 9, 16), centred on (`center_x`, `center_y`) (fractions of the
    frame) and moved inside the frame if it would stick out. `zoom` >= 1 shrinks the rectangle by that factor.
    Returns `{"x","y","w","h"}` as fractions of the source frame, rounded to 6 places, ready for `set_clip_crop`.
    Rendering a clip with this crop at an output of the same aspect fills the frame with no bars."""
    for name, value in (
        ("source size", source_w),
        ("source size", source_h),
        ("aspect", aspect_w),
        ("aspect", aspect_h),
    ):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise TimelineError(f"{name} must be above zero")
    for name, value in (("center_x", center_x), ("center_y", center_y)):
        if isinstance(value, bool) or not 0 <= value <= 1:
            raise TimelineError(f"{name} must be between 0 and 1")
    if isinstance(zoom, bool) or not math.isfinite(zoom) or zoom < 1:
        raise TimelineError("zoom must be 1 or more (1 = the largest rectangle, 2 = half as wide)")
    target = aspect_w / aspect_h
    if source_w / source_h > target:  # the source is wider than the target: full height, narrower
        h, w = 1.0, source_h * target / source_w
    else:  # taller than the target: full width, shorter
        w, h = 1.0, source_w / target / source_h
    w, h = w / zoom, h / zoom
    x = min(max(center_x - w / 2, 0.0), 1 - w)
    y = min(max(center_y - h / 2, 0.0), 1 - h)
    w, h = round(w, 6), round(h, 6)
    x, y = round(min(x, 1 - w), 6), round(min(y, 1 - h), 6)
    try:
        return check_crop(x, y, w, h)
    except RuleViolation:
        raise TimelineError(
            f"zoom {zoom:g} makes the crop smaller than the minimum ({CROP_MIN:g} of the frame)"
        ) from None


def parse_aspect(text: str) -> tuple[float, float]:
    """`"9:16"` (or `"1.5:1"`) as (9.0, 16.0); `TimelineError` for anything else."""
    parts = text.strip().split(":") if isinstance(text, str) else []
    try:
        a, b = (float(p) for p in parts)
    except ValueError:
        a = b = 0.0
    if len(parts) != 2 or not (math.isfinite(a) and math.isfinite(b)) or a <= 0 or b <= 0:
        raise TimelineError(f"aspect must look like 9:16, not {text!r}")
    return a, b


def _crop_changes(track: Track, clip: Clip, crop: object) -> dict[str, float]:
    """The `Clip` field changes for `crop` (a dict, or None for the full frame) on `clip`."""
    if clip.kind == "text":
        raise RuleViolation("crop_not_applicable", "a text clip has no picture to crop")
    if track.kind == "audio":
        raise RuleViolation("crop_not_applicable", "an audio clip has no picture to crop")
    if crop is None:
        return {"crop_x": 0.0, "crop_y": 0.0, "crop_w": 1.0, "crop_h": 1.0}
    if not isinstance(crop, dict) or set(crop) != {"x", "y", "w", "h"}:
        raise RuleViolation("crop_invalid", CROP_ERROR)
    c = check_crop(crop["x"], crop["y"], crop["w"], crop["h"])
    return {"crop_x": c["x"], "crop_y": c["y"], "crop_w": c["w"], "crop_h": c["h"]}


def set_clip_properties(
    project: Project, clip_id: str, **properties: float | bool | dict[str, float] | None
) -> Project:
    """Change a clip's gain/mute, overlay placement, colour adjustment or crop.

    Only the names in `CLIP_PROPERTY_RANGES` plus `muted` and `crop` are accepted, and a
    value outside its range is an error rather than being clamped. `crop` is a dict
    `{"x","y","w","h"}` (see `check_crop`) or None to remove the crop.
    """
    if not properties:
        raise TimelineError("no properties given")
    track, i = find_clip(project, clip_id)
    changes: dict[str, float | bool] = {}
    for name, value in properties.items():
        if name == "muted":
            changes[name] = bool(value)
        elif name == "crop":
            changes.update(_crop_changes(track, track.clips[i], value))
        elif name in CLIP_PROPERTY_RANGES:
            low, high = CLIP_PROPERTY_RANGES[name]
            if isinstance(value, bool) or not low <= value <= high:
                raise TimelineError(f"{name} must be between {low:g} and {high:g}")
            changes[name] = float(value)
        else:
            valid = ", ".join(["muted", "crop", *CLIP_PROPERTY_RANGES])
            raise TimelineError(f"unknown clip property {name!r}; choose from {valid}")
    clips = list(track.clips)
    clips[i] = replace(clips[i], **changes)
    return _with_clips(project, track, clips)


def add_text_clip(
    project: Project,
    track_id: str,
    text: str,
    duration: float,
    *,
    start: float = 0.0,
    font_size: float = 0.08,
    color: str = "#ffffff",
    x: float = 0.5,
    y: float = 0.85,
) -> tuple[Project, Clip]:
    """Put a text title on a free video track, drawn for `duration` seconds from `start`."""
    track = find_track(project, track_id)
    if track.kind != "video" or track.magnetic:
        raise RuleViolation("text_target", "text goes on an overlay video track, not the main or an audio track")
    if duration < MIN_CLIP:
        raise RuleViolation("too_short", f"a clip must be at least {MIN_CLIP:g}s long")
    clip = Clip(id=new_id(), source="", in_point=0.0, out_point=duration, start=start, kind="text", x=x, y=y)
    clip = _text_properties(clip, text=text, font_size=font_size, color=color)
    new = _with_clips(project, track, [*track.clips, clip])
    return new, find_clip_in(new, clip.id)


def _text_properties(
    clip: Clip, *, text: str | None = None, font_size: float | None = None, color: str | None = None
) -> Clip:
    changes: dict[str, str | float] = {}
    if text is not None:
        if not text.strip():
            raise TimelineError("text can't be empty")
        changes["text"] = text
    if font_size is not None:
        if not FONT_SIZE_RANGE[0] <= font_size <= FONT_SIZE_RANGE[1]:
            raise TimelineError("font_size must be between 0.01 and 0.5 (a fraction of the frame height)")
        changes["font_size"] = float(font_size)
    if color is not None:
        if not _COLOR_RE.match(color):
            raise TimelineError("color must look like #rrggbb")
        changes["color"] = color
    return replace(clip, **changes)


def set_text_properties(
    project: Project,
    clip_id: str,
    *,
    text: str | None = None,
    font_size: float | None = None,
    color: str | None = None,
) -> Project:
    track, i = find_clip(project, clip_id)
    if track.clips[i].kind != "text":
        raise RuleViolation("not_text_clip", "only text clips have text properties")
    if text is None and font_size is None and color is None:
        raise TimelineError("no properties given")
    clips = list(track.clips)
    clips[i] = _text_properties(clips[i], text=text, font_size=font_size, color=color)
    return _with_clips(project, track, clips)


def add_track(project: Project, kind: str, *, magnetic: bool = False) -> tuple[Project, Track]:
    """Append a new empty track on top of the stack. Extra tracks are free
    (clips placed by start time) unless `magnetic` is asked for."""
    if kind not in TRACK_KINDS:
        raise TimelineError(f"unknown track kind {kind!r}; choose from {', '.join(TRACK_KINDS)}")
    track = Track(id=new_id(), kind=kind, magnetic=magnetic)
    return replace(project, tracks=(*project.tracks, track)), track


def remove_track(project: Project, track_id: str) -> Project:
    track = find_track(project, track_id)
    check_unlocked(track)
    if track.kind == "video" and sum(t.kind == "video" for t in project.tracks) == 1:
        raise RuleViolation("last_video_track", "a project needs at least one video track")
    return replace(project, tracks=tuple(t for t in project.tracks if t.id != track_id))


def move_track(project: Project, track_id: str, to_index: int) -> Project:
    """Reorder tracks; list order is stacking order (later draws on top)."""
    find_track(project, track_id)
    tracks = list(project.tracks)
    if not 0 <= to_index < len(tracks):
        raise RuleViolation("index_range", f"index {to_index} out of range (0..{len(tracks) - 1})")
    tracks.insert(to_index, tracks.pop(next(i for i, t in enumerate(tracks) if t.id == track_id)))
    return replace(project, tracks=tuple(tracks))


def place_clip(project: Project, clip_id: str, *, start: float | None = None, track_id: str | None = None) -> Project:
    """Put a clip at `start` seconds on a free track, optionally moving it to
    another free track of the same kind. Refuses overlaps."""
    src, i = find_clip(project, clip_id)
    clip = src.clips[i]
    dest = src if track_id is None else find_track(project, track_id)
    if dest.kind != src.kind:
        raise RuleViolation("kind_mismatch", f"can't move a {src.kind} clip onto a {dest.kind} track")
    if dest.magnetic:
        raise RuleViolation("main_track_dest", "the main track keeps its clips back to back; use move_clip or add_clip")
    moved = replace(clip, start=clip.start if start is None else start)
    if dest.id == src.id:
        clips = [moved if c.id == clip_id else c for c in src.clips]
        return _with_clips(project, src, clips)
    project = _with_clips(project, src, [c for c in src.clips if c.id != clip_id])
    dest = find_track(project, dest.id)
    return _with_clips(project, dest, [*dest.clips, moved])


def check_range(in_point: float, out_point: float, source_duration: float) -> None:
    """Raise `RuleViolation` unless `in_point`..`out_point` is a legal part of a source `source_duration` long."""
    if in_point < 0:
        raise RuleViolation("in_negative", "in point can't be negative")
    if out_point > source_duration + 1e-6:
        raise RuleViolation(
            "out_past_end", f"out point {out_point:g}s is past the end of the source ({source_duration:g}s)"
        )
    if out_point - in_point < MIN_CLIP:
        raise RuleViolation("too_short", f"a clip must be at least {MIN_CLIP:g}s long")


# --- track flags (lane header toggles) -------------------------------------


def set_track_flags(
    project: Project,
    track_id: str,
    *,
    locked: bool | None = None,
    hidden: bool | None = None,
    muted: bool | None = None,
) -> Project:
    """Set a track's `locked`, `hidden` and `muted` flags; a flag left as None is unchanged. Allowed on a locked
    track (that is how it is unlocked). The main track (the first video track) cannot be hidden: the picture would
    be gone from the export, which is what deleting its clips is for."""
    track = find_track(project, track_id)
    if locked is None and hidden is None and muted is None:
        raise TimelineError("no flags given")
    if hidden and track.kind == "video" and track_id == _main_id(project):
        raise RuleViolation("main_track_hidden", "the main video track cannot be hidden")
    if hidden and track.kind == "audio":
        raise RuleViolation("hidden_audio_track", "only video tracks can be hidden; mute an audio track instead")
    changes = {k: v for k, v in (("locked", locked), ("hidden", hidden), ("muted", muted)) if v is not None}
    return _replace_track(project, replace(track, **changes))


def _main_id(project: Project) -> str | None:
    return next((t.id for t in project.tracks if t.kind == "video"), None)


def for_render(project: Project) -> Project:
    """The project as the export sees it: hidden tracks are left out and the clips of a muted track are muted (a
    muted clip contributes silence, see `render._gain`). A project without flags comes back unchanged."""
    if not any(t.hidden or t.muted for t in project.tracks):
        return project
    tracks = []
    for t in project.tracks:
        if t.hidden:
            continue
        if t.muted:
            t = replace(t, clips=tuple(replace(c, muted=True) for c in t.clips))
        tracks.append(t)
    return replace(project, tracks=tuple(tracks))
