"""Headless operations on a video's split points (the main track of its timeline project).

This is the single place the editor's rules for splitting, moving, labeling
and exporting segments live, so the browser editor (`editor/app.py`), the
project-aware CLI subcommands (`cli.py`) and the MCP server (`mcp_server.py`)
all call the same code and can never disagree about what, say, a valid
export looks like.

The model mirrors the frontend's (see `editor/static/state.js`): a project is
a contiguous list of segments from the first to the last song, so "split N"
is just the shared boundary between segment N and N+1. Segment and split
numbers are 1-based, matching the editor's segment table. Nothing here
persists anything except through a `ProjectStore` (locally, the video's timeline project,
the sidecar file next to the video, is the one source of truth — there is no running server to
talk to). A project is addressed by its id in the store, not by a file path.

Every function that edits a project is pure over a segment list and returns a
new one; `save_segments` is what writes it. Bad input raises `ProjectError`
(or a subclass), never a transport-specific exception, so each caller can
map it to its own error style.
"""

from __future__ import annotations

import base64
import binascii
import itertools
import json
import posixpath
import re
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from split_video import assets, split_model
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache, SilenceCache
from split_video.errors import ProjectError, RevisionConflictError  # noqa: F401  (re-exported)
from split_video.ffmpeg import ProbeError, extract_segment, iter_pcm_chunks
from split_video.jobs import CancelToken, JobCancelledError
from split_video.media_store import MediaStore
from split_video.naming import build_manifest, manifest_text, resolve_export_filename
from split_video.segments import Segment, compute_segments
from split_video.store import ProjectStore, SavedSegment, Versioned
from split_video.timeline import Project

# Minimum gap kept between any two boundaries, so an add/move can never
# produce a zero- or negative-length segment. Matches `MIN_GAP` in state.js.
MIN_GAP = 0.05

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Sentinel so `update_segment` can tell "leave unchanged" apart from "clear".
UNSET: Any = object()


class ExportConflictError(ProjectError):
    """Output files already exist and overwriting wasn't asked for."""


# --- loading / saving -------------------------------------------------------
#
# A recording's splits live in its timeline project (ADR 0003): segment n is clip n of the main track, see
# `split_model.py`. A legacy split project is migrated by these functions, in memory on every read and onto disk
# with the first write; the store only keeps the files. One revision number covers both (the store's
# `load_video_timeline`), so a write's `base_revision` means what it always did.


def _stored_segments(store: ProjectStore, project_id: str) -> Versioned[list[SavedSegment] | None]:
    """What is saved for the video: its timeline project's split view, else a legacy split project's
    segments; None if neither can be read. (`[]` is a project that was saved with no segments.)"""
    stored = store.load_video_timeline(project_id)
    if stored.value is not None:
        return Versioned(split_model.to_segments(stored.value), stored.revision)
    return Versioned(store.load_split(project_id), stored.revision)


def current_segments(store: ProjectStore, media: MediaStore, project_id: str) -> list[SavedSegment]:
    """The saved segments for project `project_id` (the video's media id), or
    one segment covering the whole recording if nothing has been saved yet (the
    editor's skip-setup state)."""
    saved = _stored_segments(store, project_id).value
    if saved:
        return saved
    return [SavedSegment(start=0.0, end=media.probe(project_id).duration)]


def video_timeline(store: ProjectStore, media: MediaStore, project_id: str) -> Project:
    """The video's timeline project as a value (read-only, nothing is written): its saved timeline, a legacy split
    project migrated in memory, or the whole recording as one clip, with the clips linked to assets."""
    stored = store.load_video_timeline(project_id).value
    return _unified(store, media, project_id, stored, current_segments(store, media, project_id))


def has_project(store: ProjectStore, project_id: str) -> bool:
    return bool(_stored_segments(store, project_id).value)


def current_revision(store: ProjectStore, project_id: str) -> int:
    """The project's stored revision: 0 if nothing was ever saved."""
    return store.load_video_timeline(project_id).revision


def load_saved(store: ProjectStore, project_id: str) -> Versioned[list[SavedSegment] | None]:
    """The saved segments with the revision they were read at; the value is None if nothing was ever saved (the
    editor then proposes splits), unlike `load_versioned`, which stands in the whole recording."""
    return _stored_segments(store, project_id)


def load_versioned(store: ProjectStore, media: MediaStore, project_id: str) -> Versioned[list[SavedSegment]]:
    """`current_segments` together with the revision they were read at."""
    stored = _stored_segments(store, project_id)
    if stored.value:
        return Versioned(stored.value, stored.revision)
    return Versioned([SavedSegment(start=0.0, end=media.probe(project_id).duration)], stored.revision)


def _unified(
    store: ProjectStore, media: MediaStore | None, project_id: str, stored: Project | None, segments: list[SavedSegment]
) -> Project:
    """`stored` (or the legacy split project, or nothing) with `segments` as its main track, linked to assets."""
    if stored is None:
        legacy = store.load_split(project_id)
        stored = split_model.from_legacy(project_id, legacy) if legacy else None
    project = split_model.apply_segments(stored, project_id, segments)
    return project if media is None else replace(assets.link_project(media, project), revision=0)


def save_segments(
    store: ProjectStore,
    project_id: str,
    segments: list[SavedSegment],
    base_revision: int | None = None,
    media: MediaStore | None = None,
) -> int:
    """Replace the project's segments and return the new revision. With `base_revision`, fails
    with `RevisionConflictError` if the project has moved on since that revision. With `media` the clips are
    linked to the video's asset."""
    saved = store.update_video_timeline(
        project_id, lambda stored: _unified(store, media, project_id, stored, segments), base_revision
    )
    return saved.revision


def edit_segments(
    store: ProjectStore,
    media: MediaStore,
    project_id: str,
    change: Callable[[list[SavedSegment]], list[SavedSegment]],
    base_revision: int | None = None,
) -> Versioned[list[SavedSegment]]:
    """Load the project's segments (the whole recording as one if nothing is saved), apply `change`
    and save the result, all atomically under the store's write lock, so concurrent edits never
    lose each other even without a `base_revision`. With one, the edit only applies if the project
    is still at that revision. Returns the saved segments and the new revision."""

    def apply(stored: Project | None) -> Project:
        if stored is not None:
            current = split_model.to_segments(stored)
        else:
            legacy = store.load_split(project_id)
            current = legacy or []
        if not current:
            current = [SavedSegment(start=0.0, end=media.probe(project_id).duration)]
        return _unified(store, media, project_id, stored, change(current))

    saved = store.update_video_timeline(project_id, apply, base_revision)
    return Versioned(split_model.to_segments(saved.value), saved.revision)


# --- describing -------------------------------------------------------------


def describe_segments(segments: list[SavedSegment]) -> list[dict[str, Any]]:
    """JSON-ready rows, numbered from 1, for CLI `--json` and MCP results."""
    return [
        {
            "index": i + 1,
            "start": round(s.start, 3),
            "end": round(s.end, 3),
            "duration": round(s.end - s.start, 3),
            "label": s.label,
            "color": s.color,
            "included": s.included,
            "export_name": s.export_name,
        }
        for i, s in enumerate(segments)
    ]


# --- proposing --------------------------------------------------------------


def contiguous(computed: list[Segment]) -> list[SavedSegment]:
    """Collapse silence-trimmed segments into the editor's contiguous model.

    `compute_segments` leaves the trimmed silence between songs as gaps; the
    editor keeps only the boundaries (each song's end), exactly like
    `loadSegments` in state.js, so a later hand-edit has a single split point
    to drag rather than a gap.
    """
    if not computed:
        return []
    ends = [s.end for s in computed[:-1]]
    starts = [computed[0].start, *ends]
    all_ends = [*ends, computed[-1].end]
    return [SavedSegment(start=a, end=b) for a, b in zip(starts, all_ends)]


def propose_segments(
    media: MediaStore,
    source: str,
    *,
    silence_threshold: float = -35.0,
    min_silence_duration: float = 2.0,
    min_song_length: float = 30.0,
    padding: float = 0.15,
) -> list[SavedSegment]:
    """Splits proposed by silence detection — the same scan + post-processing
    the editor's setup step runs (and cached the same way, so a later `edit`
    reuses the scan). Does not save; pair with `save_segments`."""
    duration = media.probe(source).duration
    silences = SilenceCache(media, source).get_raw_silences(silence_threshold)
    computed = compute_segments(silences, duration, min_silence_duration, min_song_length, padding)
    return contiguous(computed)


# --- editing ----------------------------------------------------------------


def _check_split_number(segments: list[SavedSegment], split: int) -> None:
    if not 1 <= split <= len(segments) - 1:
        raise ProjectError(
            f"split {split} doesn't exist: there {'is' if len(segments) == 2 else 'are'} "
            f"{len(segments) - 1} split(s)" + (" (a single-segment project has none)" if len(segments) == 1 else "")
        )


def _check_segment_number(segments: list[SavedSegment], number: int) -> None:
    if not 1 <= number <= len(segments):
        raise ProjectError(f"segment {number} doesn't exist: there are {len(segments)} segment(s)")


def add_split(segments: list[SavedSegment], at: float) -> tuple[list[SavedSegment], int]:
    """Split the segment containing `at` in two. Returns the new list and the
    new split's 1-based number. Both halves keep the label, color and
    included flag; a custom export name resets, since it can't meaningfully
    apply to both halves (same rule as the editor)."""
    first, last = segments[0].start, segments[-1].end
    if at <= first + MIN_GAP or at >= last - MIN_GAP:
        raise ProjectError(f"{at:g}s is outside the editable span ({first:g}s to {last:g}s)")
    for seg in segments[:-1]:
        if abs(seg.end - at) < MIN_GAP:
            raise ProjectError(f"{at:g}s is too close to the existing split at {seg.end:g}s")
    index = next(i for i, s in enumerate(segments) if s.start <= at < s.end)
    seg = segments[index]
    left = replace(seg, end=at, export_name=None)
    right = replace(seg, start=at, export_name=None)
    return [*segments[:index], left, right, *segments[index + 1 :]], index + 1


def remove_split(segments: list[SavedSegment], split: int) -> list[SavedSegment]:
    """Merge segment `split` with the one after it, keeping the earlier
    segment's metadata (same rule as the editor)."""
    _check_split_number(segments, split)
    i = split - 1
    merged = replace(segments[i], end=segments[i + 1].end)
    return [*segments[:i], merged, *segments[i + 2 :]]


def move_split(segments: list[SavedSegment], split: int, to: float) -> tuple[list[SavedSegment], float]:
    """Move split `split` to `to`, clamped so it never crosses a neighbor.
    Returns the new list and the position actually applied."""
    _check_split_number(segments, split)
    i = split - 1
    lower = segments[i].start
    upper = segments[i + 1].end
    clamped = min(max(to, lower + MIN_GAP), upper - MIN_GAP)
    moved = [
        *segments[:i],
        replace(segments[i], end=clamped),
        replace(segments[i + 1], start=clamped),
        *segments[i + 2 :],
    ]
    return moved, clamped


def update_segment(
    segments: list[SavedSegment],
    number: int,
    *,
    label: str | None = None,
    color: str | None = UNSET,
    included: bool | None = None,
    export_name: str | None = UNSET,
) -> list[SavedSegment]:
    """Change one segment's metadata. Omitted fields are left alone; pass an
    empty string for `color` or `export_name` to clear it."""
    _check_segment_number(segments, number)
    changes: dict[str, Any] = {}
    if label is not None:
        changes["label"] = label.strip()
    if color is not UNSET:
        if color in (None, ""):
            changes["color"] = None
        elif _COLOR_RE.match(color):
            changes["color"] = color.lower()
        else:
            raise ProjectError(f"color must look like #rrggbb, got {color!r}")
    if included is not None:
        changes["included"] = included
    if export_name is not UNSET:
        changes["export_name"] = (export_name or "").strip() or None
    i = number - 1
    return [*segments[:i], replace(segments[i], **changes), *segments[i + 1 :]]


# --- exporting --------------------------------------------------------------


@dataclass(frozen=True)
class ExportPlan:
    source: str  # media ids, as in the `MediaStore`
    segments: list[tuple[float, float]]
    filenames: list[str]
    output_dir: str


def select_for_export(segments: list[SavedSegment], only: list[int] | None = None) -> list[SavedSegment]:
    """The segments to write: those ticked "include" in the project, or
    exactly the numbered ones when `only` is given (which overrides the
    include flag — naming a segment explicitly means you want it)."""
    if only is None:
        return [s for s in segments if s.included]
    for number in only:
        _check_segment_number(segments, number)
    return [segments[n - 1] for n in sorted(set(only))]


def validate_pairs(pairs: list[tuple[float, float]]) -> None:
    if not pairs:
        raise ProjectError("segments list must not be empty")
    if sorted(pairs, key=lambda p: p[0]) != pairs:
        raise ProjectError("segments must be sorted by start time")
    for start, end in pairs:
        if end <= start:
            raise ProjectError(f"segment end must be after start: ({start}, {end})")
    for (_, end), (next_start, _) in itertools.pairwise(pairs):
        if next_start < end:
            raise ProjectError("segments must not overlap")


def plan_export(
    media: MediaStore,
    source: str,
    segments: list[tuple[float, float, str | None]],
    *,
    output_format: str | None = None,
    overwrite: bool = False,
) -> ExportPlan:
    """Validate an export and work out where each file lands. `segments` are
    `(start, end, custom_name)` triples, already narrowed to what should be
    written. Raises `ExportConflictError` if files exist and `overwrite` is off."""
    pairs = [(start, end) for start, end, _ in segments]
    validate_pairs(pairs)
    output_dir = media.export_dir(source)
    name = media.name(source)
    stem, suffix = posixpath.splitext(name)
    ext = suffix if output_format is None else f".{output_format.lstrip('.')}"
    filenames = [
        resolve_export_filename(i + 1, len(pairs), stem, ext, custom) for i, (_, _, custom) in enumerate(segments)
    ]
    if len(set(filenames)) != len(filenames):
        raise ProjectError("export names must be unique across segments")
    if not overwrite:
        conflicts = [f for f in filenames if media.exists(media.join(output_dir, f))]
        if conflicts:
            raise ExportConflictError(
                f"{len(conflicts)} output file(s) already exist (pass overwrite to replace): {', '.join(conflicts)}"
            )
    return ExportPlan(source=source, segments=pairs, filenames=filenames, output_dir=output_dir)


def run_export(
    media: MediaStore,
    plan: ExportPlan,
    *,
    precise: bool = False,
    output_format: str | None = None,
    write_manifest_file: bool = True,
    parameters: dict[str, Any] | None = None,
    on_file_start: Callable[[str], None] | None = None,
    on_file_done: Callable[[str], None] | None = None,
    cancel: CancelToken | None = None,
) -> tuple[list[str], str | None]:
    """Write the planned files (and `manifest.json`). Blocking; the editor
    runs it as a job, the CLI and MCP server call it inline.
    Returns the written media ids and the manifest's, if one was written.

    With a `cancel` token (a job's `JobContext`), cancelling stops before the next file or
    terminates the ffmpeg extracting the current one (its partial file is deleted) and raises
    `JobCancelledError`. Files already finished stay, and no manifest is written."""
    objs = [Segment(index=i + 1, start=a, end=b) for i, (a, b) in enumerate(plan.segments)]
    written: list[str] = []
    for seg, filename in zip(objs, plan.filenames):
        out_id = media.join(plan.output_dir, filename)
        if cancel is not None:
            cancel.raise_if_cancelled()
        if on_file_start:
            on_file_start(filename)
        try:
            with media.local_path(plan.source) as source, media.write_local(out_id) as out_path:
                extract_segment(source, seg.start, seg.end, out_path, precise, output_format, cancel=cancel)
        except JobCancelledError:
            media.delete(out_id)
            raise
        written.append(out_id)
        if on_file_done:
            on_file_done(out_id)

    manifest_path = None
    if write_manifest_file:
        data = build_manifest(
            source_name=media.name(plan.source),
            segments=objs,
            filenames=plan.filenames,
            parameters=parameters if parameters is not None else {"precise": precise},
            generated_at=datetime.now(timezone.utc),
        )
        manifest_path = media.join(plan.output_dir, "manifest.json")
        media.write_bytes(manifest_path, manifest_text(data).encode())
    return written, manifest_path


# --- audio analysis ---------------------------------------------------------


def run_analysis(
    media: MediaStore,
    source: str,
    cache: ClassificationCache,
    thresholds: dict[str, float],
    on_progress: Callable[[int, int], None] | None = None,
    cancel: CancelToken | None = None,
) -> None:
    """Run YAMNet over `source` and store the result in `cache` (which writes
    the sidecar cache file). Blocking and potentially slow on a long recording.

    With a `cancel` token (a job's `JobContext`) it stops, raising `JobCancelledError` and
    storing nothing, while decoding the audio (ffmpeg is terminated) or between inference frames.
    `on_progress(frames_done, frames_total)` follows the real decode position (the total is estimated from
    the probed duration and corrected at the end)."""
    if cancel is not None:
        cancel.raise_if_cancelled()
    model, labels = classify.get_model_and_labels()

    def frame_done(completed: int, total: int) -> None:
        if cancel is not None:
            cancel.raise_if_cancelled()
        if on_progress:
            on_progress(completed, total)

    try:
        expected_samples: int | None = int(media.probe(source).duration * classify.SAMPLE_RATE)
    except ProbeError:
        expected_samples = None  # only used to size the progress total
    # Streamed: the audio is decoded a few seconds at a time while the model runs, so memory does not
    # grow with the recording.
    with (
        media.local_path(source) as path,
        closing(iter_pcm_chunks(path, classify.SAMPLE_RATE, cancel=cancel)) as chunks,
    ):
        regions, frame_bucket_scores, total_duration = classify.classify_stream(
            chunks, model, labels, thresholds, frame_done, expected_samples
        )
    cache.store_result(frame_bucket_scores, total_duration, regions)


REGION_PAGE_DEFAULT = 200
# about 54 KB of MCP text at 500 regions: under the 64 KB tool-response cap (docs/tool-response-caps.md)
REGION_PAGE_MAX = 500


def _page_cursor(position: int, total: int) -> str:
    raw = json.dumps({"i": position, "n": total}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _page_start(cursor: str | None, total: int) -> int:
    if not cursor:
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        position, count = int(data["i"]), int(data["n"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise ValueError("cursor is not one returned by this tool: start without it") from None
    if count != total or not 0 <= position <= total:
        raise ValueError("cursor no longer matches the analysis (it was made again): start without it")
    return position


def page_classification(
    cache: ClassificationCache, limit: int | None = None, cursor: str | None = None
) -> dict[str, Any]:
    """`summarize_classification` bounded for a tool result: the whole-recording summary (`seconds_by_bucket`,
    `regions_by_bucket`, `total_seconds`, `total_regions`) plus one page of at most `limit` regions (default
    `REGION_PAGE_DEFAULT`, at most `REGION_PAGE_MAX`) from `cursor`, with `count`, `truncated`, `next_cursor` and
    `notices`. Concatenating the pages gives exactly the `regions` of `summarize_classification`."""
    if limit is None:
        limit = REGION_PAGE_DEFAULT
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= REGION_PAGE_MAX:
        raise ValueError(f"limit must be a whole number from 1 to {REGION_PAGE_MAX}")
    full = summarize_classification(cache)
    regions = full.pop("regions")
    total = len(regions)
    at = _page_start(cursor, total)
    page = regions[at : at + limit]
    end = at + len(page)
    by_bucket: dict[str, int] = {}
    for r in regions:
        by_bucket[r["bucket"]] = by_bucket.get(r["bucket"], 0) + 1
    more = end < total
    notices = []
    if more:
        notices.append(
            f"regions {at + 1}-{end} of {total}: call again with cursor=next_cursor to read the rest "
            "(seconds_by_bucket and regions_by_bucket already cover the whole recording)"
        )
    return {
        **full,
        "total_seconds": round(sum(full["seconds_by_bucket"].values()), 1),
        "regions_by_bucket": dict(sorted(by_bucket.items())),
        "total_regions": total,
        "count": len(page),
        "regions": page,
        "truncated": at > 0 or more,
        "next_cursor": _page_cursor(end, total) if more else None,
        "notices": notices,
    }


def summarize_classification(cache: ClassificationCache) -> dict[str, Any]:
    """Compact, JSON-ready view of an analysis: seconds per bucket plus the
    merged regions (start/end/bucket/score)."""
    regions = cache.get_regions()
    seconds: dict[str, float] = {}
    for r in regions:
        seconds[r.bucket] = seconds.get(r.bucket, 0.0) + (r.end - r.start)
    return {
        "analyzed": cache.is_analyzed,
        "seconds_by_bucket": {k: round(v, 1) for k, v in sorted(seconds.items())},
        "regions": [
            {"start": round(r.start, 2), "end": round(r.end, 2), "bucket": r.bucket, "score": round(r.score, 2)}
            for r in regions
        ],
    }
