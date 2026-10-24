"""A recording's splits as a timeline project (ADR 0003, SPL-116).

The timeline project is the one model: a recording opened for splitting is a timeline whose main video track
holds the video as clips, one per segment, back to back. This module is the pure mapping between that and the
segment lists the split tools (`project.py`) work on, and from a legacy split project into a timeline. It
touches no files and no media; see `docs/decisions/unified-project-model.md` for the field mapping.

The split view of a timeline is its first video track's media clips in order: segment `n` is clip `n`, its
`start`/`end` the clip's `in`/`out` in the source. Writing a segment list back keeps what is not a split
concern (other tracks, clip properties such as gain or crop of segments whose range is unchanged).
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from split_video.errors import ProjectError
from split_video.store import SavedSegment
from split_video.timeline import Clip, Project, Track, new_id
from split_video.timeline_ops import normalize_track


def _stable_id(*parts: object) -> str:
    return hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:8]


def main_track_index(project: Project) -> int | None:
    """The index of the main track: the first video track, or None if there is none."""
    return next((i for i, t in enumerate(project.tracks) if t.kind == "video"), None)


def to_segments(project: Project) -> list[SavedSegment]:
    """The project's split view: the main track's media clips as segments, in order."""
    index = main_track_index(project)
    if index is None:
        return []
    return [
        SavedSegment(
            start=c.in_point,
            end=c.out_point,
            label=c.label,
            color=c.label_color,
            included=c.included,
            export_name=c.export_name,
        )
        for c in project.tracks[index].clips
        if c.kind == "media"
    ]


def _segment_clip(clip: Clip, seg: SavedSegment) -> Clip:
    return replace(
        clip,
        in_point=seg.start,
        out_point=seg.end,
        label=seg.label,
        label_color=seg.color,
        included=seg.included,
        export_name=seg.export_name,
    )


def from_legacy(video_id: str, segments: list[SavedSegment]) -> Project:
    """A timeline project from a legacy split project's segments: the video as consecutive clips of one main
    track. Ids are derived from the video id and position, so reading the same legacy file twice gives the same
    project. The clips carry `source` only; the caller links them to the video's asset."""
    clips = [
        _segment_clip(Clip(id=_stable_id(video_id, "clip", i), source=video_id, in_point=0.0, out_point=0.0), seg)
        for i, seg in enumerate(segments)
    ]
    track = Track(id=_stable_id(video_id, "main"), kind="video", clips=tuple(clips), magnetic=True)
    return Project(tracks=(normalize_track(track),))


def _source_key(clip: Clip) -> str:
    return clip.asset or clip.source


def _overlap(clip: Clip, seg: SavedSegment) -> float:
    return min(clip.out_point, seg.end) - max(clip.in_point, seg.start)


def apply_segments(existing: Project | None, video_id: str, segments: list[SavedSegment]) -> Project:
    """`existing` (None: a new project) with its main track replaced by `segments`.

    A segment whose `(start, end)` matches a clip keeps that clip as it is (id, gain, crop, transitions, ...). A
    changed segment takes the other properties of the clip it overlaps most, as a cut, and that clip's id if no
    other segment has claimed it. Other tracks are left alone. Raises `ProjectError` if the main track holds
    anything but media clips of one source (the timeline was edited beyond split points)."""
    project = existing if existing is not None else Project(tracks=())
    index = main_track_index(project)
    old: list[Clip] = list(project.tracks[index].clips) if index is not None else []
    if any(c.kind != "media" or _source_key(c) != _source_key(old[0]) for c in old):
        raise ProjectError(
            "this project's main track holds more than the recording's own clips (other sources or text); "
            "its splits can't be edited here, use the compose editor"
        )
    template = old[0] if old else Clip(id="", source=video_id, in_point=0.0, out_point=0.0)
    claimed: set[str] = set()
    clips: list[Clip] = []
    for seg in segments:
        exact = next(
            (c for c in old if c.id not in claimed and (c.in_point, c.out_point) == (seg.start, seg.end)), None
        )
        if exact is not None:
            claimed.add(exact.id)
            clips.append(_segment_clip(exact, seg))
            continue
        best = max(old, key=lambda c: _overlap(c, seg), default=None)
        if best is None or _overlap(best, seg) <= 0:
            best = None
        base = best if best is not None else template
        clip_id = best.id if best is not None and best.id not in claimed else new_id()
        claimed.add(clip_id)
        clips.append(_segment_clip(replace(base, id=clip_id, transition="cut", transition_duration=0.0), seg))
    track = (
        replace(project.tracks[index], clips=tuple(clips))
        if index is not None
        else Track(id=new_id(), kind="video", clips=tuple(clips), magnetic=True)
    )
    track = normalize_track(track)
    tracks = (
        (track, *project.tracks)
        if index is None
        else tuple(track if i == index else t for i, t in enumerate(project.tracks))
    )
    return replace(project, tracks=tracks)
