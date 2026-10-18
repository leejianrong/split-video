"""Editing rules for a timeline `Project`.

Like `project.py` for the split editor, this is the one place the rules
live, so the web UI, the CLI and the MCP server can't disagree. Every
function is pure: it takes a `Project` and returns a new one; the caller
saves it. Bad input raises `TimelineError`.

Phase 2 keeps each video track gap-free: clips sit back to back in list
order, and a crossfade overlaps a clip with the one before it. All of that
layout lives in `normalize_track`, which is the single function to change
when clips become freely placeable.
"""

from __future__ import annotations

from dataclasses import replace

from split_video.timeline import (
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
    """Lay clips out back to back and make their transitions consistent.

    The first clip is always a cut. A crossfade is clamped so it never
    outlasts either neighbour, counting the fade a clip already spends on its
    other side. A crossfade of zero length becomes a cut.
    """
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
) -> tuple[Project, Clip]:
    """Insert a clip of `source` (which is `source_duration` long) at `index`
    on the track, or at the end. Returns the new project and the new clip."""
    track = find_track(project, track_id)
    out = source_duration if out_point is None else out_point
    _check_range(in_point, out, source_duration)
    clip = Clip(id=new_id(), source=source, in_point=in_point, out_point=out)
    clips = list(track.clips)
    if index is None:
        index = len(clips)
    if not 0 <= index <= len(clips):
        raise TimelineError(f"index {index} out of range (0..{len(clips)})")
    clips.insert(index, clip)
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
    clips[i] = replace(clip, in_point=new_in, out_point=new_out)
    return _with_clips(project, track, clips)


def move_clip(project: Project, clip_id: str, to_index: int) -> Project:
    """Move a clip to `to_index` (0-based) among its track's clips."""
    track, i = find_clip(project, clip_id)
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


def _check_range(in_point: float, out_point: float, source_duration: float) -> None:
    if in_point < 0:
        raise TimelineError("in point can't be negative")
    if out_point > source_duration + 1e-6:
        raise TimelineError(f"out point {out_point:g}s is past the end of the source ({source_duration:g}s)")
    if out_point - in_point < MIN_CLIP:
        raise TimelineError(f"a clip must be at least {MIN_CLIP:g}s long")
