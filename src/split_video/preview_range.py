"""A short, low-resolution mp4 of a stretch of the edited timeline: what to watch to judge transitions and pacing.

`plan_range` checks a request and names the chunk; `lookup` answers from the cache; `render_range` makes it. The chunk is
rendered by the export's own code (`render.prepare` and `render.build_plan`, as `preview.py` does for one frame): the
project is cut to the window `[start, end)` (`preview.window`), so ffmpeg seeks each source to where it is at `start`
and decodes only the chunk however long the timeline is. All tracks are drawn and all audio is mixed (main track,
overlays, audio tracks; clip gain and mute included). Captions are a render option and are not drawn.

Size and codec: the picture is `height` pixels high (default 480, at most 720, never above the export's) with the
export's aspect ratio and frame rate; H.264 (`ultrafast`, crf 30, yuv420p, so it is Constrained Baseline, the profile
every browser decodes) and AAC 64 kbit/s stereo in an mp4 with `+faststart`, so a `<video>` plays it and seeks by Range
request as soon as the first bytes arrive. `source="auto"` reads clips' proxies where they exist (15 fps, mono audio),
`"original"` reads the files.

Length: at most `MAX_RANGE` (30 s). A chunk costs about one second of ffmpeg per 3 to 10 s of range on a typical
project and some 100 KB per second, so 30 s keeps one render to a few seconds and one file to a few MB; a longer
range is a `RangeTooLongError` (code `preview.range_too_long`) and the caller asks for consecutive chunks. A range that
runs past the end of the timeline is cut to it. If `start` falls inside a cross-fade, ffmpeg cannot start in the
middle of it, so the chunk is rendered from the fade's start and the lead-in is cut off: `start` is always exact.

Cache: chunks live in the sidecar of the `anchor` media id (the video, or `<name>.timeline` for a named project, see
`MediaStore.derived_id`), never in a central path, with a small JSON index beside them. The key is a hash of the
project revision, the range, the options and the size and mtime of every source file the chunk is rendered from (the
proxy where `source="auto"` uses one), so any edit or replaced source gives new keys and a chunk of an older revision is
never served (`find_chunk`). When a new chunk is stored, chunks of other revisions are deleted, then at most `KEEP` (8)
chunks and `MAX_BYTES` remain, least recently used first out. A chunk of a replaced source is never asked for again; it
stays until those limits push it out. A hit renders nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any

from split_video import frames, presets, preview, render
from split_video.jobs import CancelToken
from split_video.media_store import MediaError, MediaStore
from split_video.timeline import Project

MAX_RANGE = 30.0  # seconds
MIN_RANGE = 0.1
DEFAULT_HEIGHT = 480
MAX_HEIGHT = 720
KEEP = 8  # chunks kept per project
# KEEP chunks of the longest range are about 25 MB, so this only bites on unusually heavy chunks (high-motion 720p).
MAX_BYTES = 256 * 1024 * 1024
MIME = "video/mp4"
VERSION = 1  # part of the key: bump when the encoding changes
SOURCES = frames.SOURCES
CRF = 30

_INDEX_LOCK = threading.Lock()


class RangeError(frames.FrameError):
    """A bad range."""


class RangeTooLongError(frames.FrameArgumentError):
    """The range is longer than `MAX_RANGE`."""

    error_code = "preview.range_too_long"

    def __init__(self, length: float) -> None:
        super().__init__(
            f"a preview range is at most {MAX_RANGE:g} s, this one is {length:g} s: ask for consecutive ranges"
        )
        self.details = {"max": MAX_RANGE, "requested": round(length, 3)}


@dataclass(frozen=True)
class RangePlan:
    """A checked request: what will be rendered and where the chunk goes. Pass it to `lookup` and `render_range`."""

    name: str
    revision: int
    key: str
    chunk: str  # the media id of the mp4
    anchor: str
    start: float
    end: float
    width: int
    height: int
    fps: int
    source_mode: str  # "auto" or "original", as asked
    settings: presets.ExportSettings
    prepared: render.Prepared = field(repr=False)


def _index_id(media: MediaStore, anchor: str) -> str:
    return media.derived_id(anchor, "preview-index.json")


def _read_index(media: MediaStore, anchor: str) -> list[dict[str, Any]]:
    try:
        with media.open_read(_index_id(media, anchor)) as f:
            entries = json.load(f)["entries"]
        return [e for e in entries if {"key", "media", "revision", "bytes"} <= set(e)]
    except (MediaError, ValueError, KeyError, TypeError, OSError):
        return []


def _write_index(media: MediaStore, anchor: str, entries: list[dict[str, Any]]) -> None:
    media.write_bytes(_index_id(media, anchor), json.dumps({"entries": entries}).encode())


def anchor_of_project(name: str) -> str:
    """The anchor of a named timeline project."""
    return f"{name}.timeline"


def plan_range(
    project: Project,
    media: MediaStore,
    anchor: str,
    start: float,
    end: float,
    *,
    name: str = "",
    height: int | None = None,
    preset: str | None = None,
    fit: str | None = None,
    source: str = "auto",
) -> RangePlan:
    """Check the request and name the chunk. Raises `frames.FrameArgumentError` (bad height or source),
    `RangeTooLongError`, `frames.FrameTimeError` (a start that is negative or past the end), `RangeError` (end not after
    start) and `TimelineError` for what the export also refuses (no clips, a missing source)."""
    if source not in SOURCES:
        raise frames.FrameArgumentError(f"unknown source {source!r}: use auto or original")
    if height is not None and not 2 <= height <= MAX_HEIGHT:
        raise frames.FrameArgumentError(f"height must be between 2 and {MAX_HEIGHT} pixels")
    if math.isnan(start) or math.isnan(end) or start < 0:
        raise frames.FrameTimeError(f"start must be 0 or more seconds, not {start}")
    if end <= start:
        raise RangeError(f"end ({end:g} s) must be after start ({start:g} s)")
    if end - start > MAX_RANGE + 1e-9:
        raise RangeTooLongError(end - start)
    preset = presets.canvas_preset(preset, None, None, project.canvas_preset)
    settings = presets.resolve_export(preset, None, None, fit)
    prepared = render.prepare(project, media, settings)
    duration = prepared.project.duration
    if start >= duration:
        raise frames.FrameTimeError(f"start {start:g} s is past the end of the timeline, which is {duration:g} s long")
    start, end = round(start, 3), round(min(end, duration), 3)
    if end - start < MIN_RANGE:
        raise RangeError(f"the range is {end - start:g} s after the timeline's end; at least {MIN_RANGE:g} s is needed")
    out_height = preview._even(min(height or DEFAULT_HEIGHT, prepared.height))
    out_width = preview._even(out_height * prepared.width / prepared.height)
    sources = _source_stats(media, prepared.project, dict(prepared.infos), source)
    digest = hashlib.sha1(
        f"{VERSION}|{project.revision}|{start}|{end}|{out_height}|{preset}|{settings.fit}|{source}|{sources}".encode()
    ).hexdigest()[:20]
    return RangePlan(
        name, project.revision, digest, media.derived_id(anchor, f"preview-{digest}.mp4"), anchor, start, end,
        out_width, out_height, settings.fps, source, settings, prepared,
    )  # fmt: skip


def _source_stats(
    media: MediaStore, project: Project, infos: dict[str, Any], source: str
) -> list[tuple[str, int, float]]:
    """Id, size and mtime of each file the chunk is rendered from, so a replaced file changes the key."""
    if source == "auto":
        project, _ = preview.use_proxies(media, project, infos)
    ids = {c.source for t in project.tracks for c in t.clips if c.source}
    return [(i, st.size, st.mtime) for i in sorted(ids) for st in [media.stat(i)]]


def _result(plan: RangePlan, entry: dict[str, Any], media: MediaStore, *, cached: bool, source: str) -> dict[str, Any]:
    return {
        "project": plan.name,
        "revision": plan.revision,
        "key": plan.key,
        "media": plan.chunk,
        "path": media.display_path(plan.chunk),
        "mime": MIME,
        "start": plan.start,
        "end": plan.end,
        "duration": round(plan.end - plan.start, 3),
        "width": plan.width,
        "height": plan.height,
        "fps": plan.fps,
        "source": source,
        "bytes": entry["bytes"],
        "cached": cached,
    }


def lookup(media: MediaStore, plan: RangePlan) -> dict[str, Any] | None:
    """The result for `plan` if its chunk is cached (and marks it recently used), else None."""
    with _INDEX_LOCK:
        entries = _read_index(media, plan.anchor)
        hit = next((e for e in entries if e["key"] == plan.key), None)
        if hit is None or not media.is_file(plan.chunk) or media.stat(plan.chunk).size != hit["bytes"]:
            return None
        _write_index(media, plan.anchor, [*(e for e in entries if e is not hit), hit])
    return _result(plan, hit, media, cached=True, source=hit.get("source", "original"))


def find_chunk(media: MediaStore, anchor: str, key: str, revision: int) -> str | None:
    """The media id of the cached chunk `key`, only while the project is still at `revision`: after an edit the old
    key is gone for good."""
    entry = next((e for e in _read_index(media, anchor) if e["key"] == key), None)
    if entry is None or entry["revision"] != revision or not media.is_file(entry["media"]):
        return None
    return str(entry["media"])


def render_range(
    media: MediaStore,
    plan: RangePlan,
    progress: Callable[[float], None] | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """The chunk's result, rendering it unless it is cached. `progress` gets 0.0..1.0. A cancelled or failed render
    leaves no file and no index entry. Raises `ExtractError` if ffmpeg fails or stops making progress."""
    cached = lookup(media, plan)
    if cached is not None:
        return cached
    prepared = plan.prepared
    windowed, w0 = preview.window(prepared.project, plan.start, plan.end)
    seek = plan.start - w0
    infos = dict(prepared.infos)
    kind = "original"
    if plan.source_mode == "auto":
        windowed, kind = preview.use_proxies(media, windowed, infos)
    graph = render.build_plan(windowed, infos, plan.width, plan.height, prepared.font, plan.fps, plan.settings.fit)
    encoder = presets.ExportPreset(
        "preview-range", "Preview range", "Low-resolution chunk for judging motion.", plan.width, plan.height,
        plan.fps, "libx264", "ultrafast", CRF, "yuv420p", "aac", "64k", 48000, "mp4", True,
    )  # fmt: skip
    writing = False
    try:
        with ExitStack() as stack:
            inputs = [str(stack.enter_context(media.local_path(c.source))) for c in graph.clips]
            out_path = stack.enter_context(media.write_local(plan.chunk))
            writing = True
            render._run_ffmpeg(
                graph, inputs, str(out_path), plan.end - plan.start, progress, cancel, encoder, seek=seek
            )
    except BaseException:
        if writing:
            media.delete(plan.chunk)
        raise
    entry = {
        "key": plan.key,
        "media": plan.chunk,
        "revision": plan.revision,
        "bytes": media.stat(plan.chunk).size,
        "source": kind,
    }
    with _INDEX_LOCK:
        _store(media, plan, entry)
    return _result(plan, entry, media, cached=False, source=kind)


def _store(media: MediaStore, plan: RangePlan, entry: dict[str, Any]) -> None:
    """Add `entry`, then delete the chunks of other revisions, then the least recently used until `KEEP` and `MAX_BYTES`
    hold (the new chunk always stays)."""
    entries = [e for e in _read_index(media, plan.anchor) if e["key"] != plan.key]
    stale = [e for e in entries if e["revision"] != plan.revision]
    current = [e for e in entries if e["revision"] == plan.revision]
    kept = [*current, entry][-KEEP:]
    while len(kept) > 1 and sum(e["bytes"] for e in kept) > MAX_BYTES:
        kept.pop(0)
    for gone in [*stale, *(e for e in current if e not in kept)]:
        media.delete(gone["media"])
    _write_index(media, plan.anchor, kept)
