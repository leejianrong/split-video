"""Headless operations on a video's split-point project.

This is the single place the editor's rules for splitting, moving, labeling
and exporting segments live, so the browser editor (`editor/app.py`), the
project-aware CLI subcommands (`cli.py`) and the MCP server (`mcp_server.py`)
all call the same code and can never disagree about what, say, a valid
export looks like.

The model mirrors the frontend's (see `editor/static/state.js`): a project is
a contiguous list of segments from the first to the last song, so "split N"
is just the shared boundary between segment N and N+1. Segment and split
numbers are 1-based, matching the editor's segment table. Nothing here
persists anything except through `project_store`, whose sidecar file next to
the video is the one source of truth — there is no running server to talk to.

Every function that edits a project is pure over a segment list and returns a
new one; `save_segments` is what writes it. Bad input raises `ProjectError`
(or a subclass), never a transport-specific exception, so each caller can
map it to its own error style.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from split_video.editor import classify
from split_video.editor.cache import ClassificationCache, SilenceCache
from split_video.editor.project_store import SavedSegment
from split_video.editor.project_store import load as load_project
from split_video.editor.project_store import save as save_project
from split_video.ffmpeg import extract_pcm_audio, extract_segment, probe_duration
from split_video.naming import build_manifest, resolve_export_filename, write_manifest
from split_video.segments import Segment, compute_segments

# Minimum gap kept between any two boundaries, so an add/move can never
# produce a zero- or negative-length segment. Matches `MIN_GAP` in state.js.
MIN_GAP = 0.05

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Sentinel so `update_segment` can tell "leave unchanged" apart from "clear".
UNSET: Any = object()


class ProjectError(ValueError):
    """The request can't be applied (bad index, overlapping segments, ...)."""


class ExportConflictError(ProjectError):
    """Output files already exist and overwriting wasn't asked for."""


# --- loading / saving -------------------------------------------------------


def current_segments(source: Path) -> list[SavedSegment]:
    """The saved segments for `source`, or one segment covering the whole
    recording if nothing has been saved yet (the editor's skip-setup state)."""
    saved = load_project(source)
    if saved:
        return saved
    return [SavedSegment(start=0.0, end=probe_duration(source))]


def has_project(source: Path) -> bool:
    return bool(load_project(source))


def save_segments(source: Path, segments: list[SavedSegment]) -> None:
    save_project(source, segments)


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
    source: Path,
    *,
    silence_threshold: float = -35.0,
    min_silence_duration: float = 2.0,
    min_song_length: float = 30.0,
    padding: float = 0.15,
) -> list[SavedSegment]:
    """Splits proposed by silence detection — the same scan + post-processing
    the editor's setup step runs (and cached the same way, so a later `edit`
    reuses the scan). Does not save; pair with `save_segments`."""
    duration = probe_duration(source)
    silences = SilenceCache(source).get_raw_silences(silence_threshold)
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
    source: Path
    segments: list[tuple[float, float]]
    filenames: list[str]
    output_dir: Path


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
    source: Path,
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
    output_dir = source.with_name(f"{source.stem}_split")
    ext = source.suffix if output_format is None else f".{output_format.lstrip('.')}"
    filenames = [
        resolve_export_filename(i + 1, len(pairs), source.stem, ext, name) for i, (_, _, name) in enumerate(segments)
    ]
    if len(set(filenames)) != len(filenames):
        raise ProjectError("export names must be unique across segments")
    if not overwrite:
        conflicts = [f for f in filenames if (output_dir / f).exists()]
        if conflicts:
            raise ExportConflictError(
                f"{len(conflicts)} output file(s) already exist (pass overwrite to replace): {', '.join(conflicts)}"
            )
    return ExportPlan(source=source, segments=pairs, filenames=filenames, output_dir=output_dir)


def run_export(
    plan: ExportPlan,
    *,
    precise: bool = False,
    output_format: str | None = None,
    write_manifest_file: bool = True,
    parameters: dict[str, Any] | None = None,
    on_file_start: Callable[[str], None] | None = None,
    on_file_done: Callable[[str], None] | None = None,
) -> tuple[list[Path], Path | None]:
    """Write the planned files (and `manifest.json`). Blocking; the editor
    calls this on a worker thread, the CLI and MCP server call it inline.
    Returns the written paths and the manifest path, if one was written."""
    plan.output_dir.mkdir(parents=True, exist_ok=True)
    objs = [Segment(index=i + 1, start=a, end=b) for i, (a, b) in enumerate(plan.segments)]
    written: list[Path] = []
    for seg, filename in zip(objs, plan.filenames):
        out_path = plan.output_dir / filename
        if on_file_start:
            on_file_start(filename)
        extract_segment(plan.source, seg.start, seg.end, out_path, precise, output_format)
        written.append(out_path)
        if on_file_done:
            on_file_done(str(out_path))

    manifest_path = None
    if write_manifest_file:
        data = build_manifest(
            source_path=plan.source,
            segments=objs,
            filenames=plan.filenames,
            parameters=parameters if parameters is not None else {"precise": precise},
            generated_at=datetime.now(timezone.utc),
        )
        manifest_path = write_manifest(data, plan.output_dir)
    return written, manifest_path


# --- audio analysis ---------------------------------------------------------


def run_analysis(
    source: Path,
    cache: ClassificationCache,
    thresholds: dict[str, float],
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Run YAMNet over `source` and store the result in `cache` (which writes
    the sidecar cache file). Blocking and potentially slow on a long recording."""
    model, labels = classify.get_model_and_labels()
    pcm = extract_pcm_audio(source, sample_rate=classify.SAMPLE_RATE)
    regions, frame_bucket_scores, total_duration = classify.classify_audio(pcm, model, labels, thresholds, on_progress)
    cache.store_result(frame_bucket_scores, total_duration, regions)


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
