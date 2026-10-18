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

# Shortest clip the editor allows, so a trim can't produce a zero-length clip.
MIN_CLIP = 0.1


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
            raise TimelineError("a clip can't start before 0")
        if prev is not None and clip.start < prev.end - 1e-6:
            raise TimelineError(f"clip would overlap another clip on the track (until {prev.end:g}s)")
        prev = clip
    # Crossfades are a magnetic-track feature; here every clip is a plain cut.
    clips = [replace(c, transition="cut", transition_duration=0.0) for c in clips]
    return replace(track, clips=tuple(clips))


def _with_clips(project: Project, track: Track, clips: list[Clip]) -> Project:
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
) -> tuple[Project, Clip]:
    """Insert a clip of `source` (which is `source_duration` long) on a track.

    On a magnetic track the clip goes at `index` (default: the end); on a free
    track at `start` seconds (default: after the last clip). Returns the new
    project and the new clip."""
    track = find_track(project, track_id)
    out = source_duration if out_point is None else out_point
    _check_range(in_point, out, source_duration)
    clip = Clip(id=new_id(), source=source, in_point=in_point, out_point=out)
    clips = list(track.clips)
    if track.magnetic:
        if start is not None:
            raise TimelineError("this track keeps its clips back to back; use index, not start")
        if index is None:
            index = len(clips)
        if not 0 <= index <= len(clips):
            raise TimelineError(f"index {index} out of range (0..{len(clips)})")
        clips.insert(index, clip)
    else:
        if index is not None:
            raise TimelineError("this track places clips by start time; use start, not index")
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
    _check_range(new_in, new_out, source_duration)
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
        raise TimelineError("this track places clips by start time; use place_clip")
    clips = list(track.clips)
    if not 0 <= to_index < len(clips):
        raise TimelineError(f"index {to_index} out of range (0..{len(clips) - 1})")
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
        raise TimelineError("crossfades are only available on the main track")
    if transition == "cut":
        duration = 0.0
    else:
        if i == 0:
            raise TimelineError("the first clip on a track has nothing to crossfade from")
        if duration <= 0:
            raise TimelineError("a crossfade needs a duration above zero")
        prev = track.clips[i - 1]
        room_before = prev.duration - prev.transition_duration
        room_after = clip.duration - (track.clips[i + 1].transition_duration if i + 1 < len(track.clips) else 0.0)
        if duration > min(room_before, room_after) + 1e-9:
            raise TimelineError(
                f"crossfade of {duration:g}s is longer than the room the neighbouring clips leave "
                f"({min(room_before, room_after):g}s)"
            )
    clips = list(track.clips)
    clips[i] = replace(clip, transition=transition, transition_duration=duration)
    return _with_clips(project, track, clips)


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


def set_clip_properties(project: Project, clip_id: str, **properties: float | bool) -> Project:
    """Change a clip's gain/mute, overlay placement or colour adjustment.

    Only the names in `CLIP_PROPERTY_RANGES` plus `muted` are accepted, and a
    value outside its range is an error rather than being clamped.
    """
    if not properties:
        raise TimelineError("no properties given")
    changes: dict[str, float | bool] = {}
    for name, value in properties.items():
        if name == "muted":
            changes[name] = bool(value)
        elif name in CLIP_PROPERTY_RANGES:
            low, high = CLIP_PROPERTY_RANGES[name]
            if isinstance(value, bool) or not low <= value <= high:
                raise TimelineError(f"{name} must be between {low:g} and {high:g}")
            changes[name] = float(value)
        else:
            valid = ", ".join(["muted", *CLIP_PROPERTY_RANGES])
            raise TimelineError(f"unknown clip property {name!r}; choose from {valid}")
    track, i = find_clip(project, clip_id)
    clips = list(track.clips)
    clips[i] = replace(clips[i], **changes)
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
    if track.kind == "video" and sum(t.kind == "video" for t in project.tracks) == 1:
        raise TimelineError("a project needs at least one video track")
    return replace(project, tracks=tuple(t for t in project.tracks if t.id != track_id))


def move_track(project: Project, track_id: str, to_index: int) -> Project:
    """Reorder tracks; list order is stacking order (later draws on top)."""
    find_track(project, track_id)
    tracks = list(project.tracks)
    if not 0 <= to_index < len(tracks):
        raise TimelineError(f"index {to_index} out of range (0..{len(tracks) - 1})")
    tracks.insert(to_index, tracks.pop(next(i for i, t in enumerate(tracks) if t.id == track_id)))
    return replace(project, tracks=tuple(tracks))


def place_clip(project: Project, clip_id: str, *, start: float | None = None, track_id: str | None = None) -> Project:
    """Put a clip at `start` seconds on a free track, optionally moving it to
    another free track of the same kind. Refuses overlaps."""
    src, i = find_clip(project, clip_id)
    clip = src.clips[i]
    dest = src if track_id is None else find_track(project, track_id)
    if dest.kind != src.kind:
        raise TimelineError(f"can't move a {src.kind} clip onto a {dest.kind} track")
    if dest.magnetic:
        raise TimelineError("the main track keeps its clips back to back; use move_clip or add_clip")
    moved = replace(clip, start=clip.start if start is None else start)
    if dest.id == src.id:
        clips = [moved if c.id == clip_id else c for c in src.clips]
        return _with_clips(project, src, clips)
    project = _with_clips(project, src, [c for c in src.clips if c.id != clip_id])
    dest = find_track(project, dest.id)
    return _with_clips(project, dest, [*dest.clips, moved])


def _check_range(in_point: float, out_point: float, source_duration: float) -> None:
    if in_point < 0:
        raise TimelineError("in point can't be negative")
    if out_point > source_duration + 1e-6:
        raise TimelineError(f"out point {out_point:g}s is past the end of the source ({source_duration:g}s)")
    if out_point - in_point < MIN_CLIP:
        raise TimelineError(f"a clip must be at least {MIN_CLIP:g}s long")
