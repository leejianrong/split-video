"""Proxies and thumbnail strips: small, seek-friendly stand-ins for hours-long recordings.

A 90-minute stream is unpleasant to scrub or analyse at full size, so for each video asset the
engine can make two derived files, stored next to the original through the `MediaStore` (locally in
the video's `.split-video-cache/` sidecar, see `MediaStore.derived_id`). The original is only ever
read.

The proxy
---------
H.264 in MP4, built for scrubbing in a browser and for cheap visual work:

- at most 640 px wide: `trunc(min(640, iw) / 2) * 2` by an even height that keeps the aspect
  ratio; a smaller video is never upscaled;
- at most 15 fps (a source at or below that keeps its own rate);
- `-preset veryfast -crf 28`, yuv420p;
- a keyframe every 30 frames (2 s at 15 fps) with scene-cut keyframes off (`-g 30 -keyint_min 30
  -sc_threshold 0`), so a seek decodes at most 2 s of 640 px video;
- `-movflags +faststart`, so the index is at the front and a `<video>` seeks by Range request
  without downloading the file first;
- audio kept as AAC mono 64 kbit/s, so a browser can play it. Audio analysis (silence, waveform,
  YAMNet) still reads the original: re-encoded audio would change its results.

An audio-only asset has no proxy and no thumbnails; `prepare` reports it as skipped.

The thumbnail strip
-------------------
One JPEG sprite of `count` evenly spaced frames in a grid of `columns` x `rows` tiles of
`tile_width` x `tile_height` px, plus metadata so a client can find the tile for time T:
`index = min(count - 1, int(T / interval))`, then `column = index % columns`, `row = index // columns`,
and the tile's top-left corner is `(column * tile_width, row * tile_height)`. There is one tile
per `MIN_INTERVAL` (10 s) of video, at most `MAX_TILES` (240), so a long file's interval grows
instead of its sprite (10 columns, so at most 24 rows: 1600 x 2160 at 160 px per tile). Tile k
shows a frame within the source's keyframe spacing of `k * interval` (`fps=...:round=up`, which
also keeps a video shorter than one interval from producing nothing): only
keyframes are decoded (`-skip_frame nokey`), which is what lets a 3-hour file finish in a
fraction of the time it would take to decode every frame. It is read from the proxy when one is
current (keyframes every 2 s), else from the original.

Currency
--------
Each file has a `save_cache` entry (names "proxy" and "thumbnails") on the original, so a changed
original (size or mtime) makes both stale; the entry also records the asset id, this module's
`VERSION` and the size of the derived file, which must match what is stored. A partial file left
by a crash is therefore never current. A cancelled or failed run deletes what it wrote.
`prepare` is idempotent: with everything current it does nothing.

Who uses what
-------------
`visual_source(media, asset_id)` is the media id for visual work (scene detection, frame
extraction, contact sheets, previews, vision models): the proxy if it is current, else the
original. Export and render never call it: they read the original. Audio analysis does not either.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from split_video import assets
from split_video.ffmpeg import ExtractError, _require_binary, run_with_progress
from split_video.jobs import CancelToken
from split_video.media_store import MediaError, MediaStore

VERSION = 1

PROXY_NAME = "proxy"  # cache name of the proxy's entry
THUMBS_NAME = "thumbnails"
PROXY_SUFFIX = "proxy.mp4"
THUMBS_SUFFIX = "thumbs.jpg"

PROXY_MAX_WIDTH = 640
PROXY_MAX_FPS = 15
PROXY_CRF = 28
PROXY_KEYFRAME_FRAMES = 30

TILE_WIDTH = 160
COLUMNS = 10
MIN_INTERVAL = 10.0  # seconds of video per tile, at least
MAX_TILES = 240

# A job's progress is this many steps: the proxy takes the first part, the thumbnails the rest.
STEPS = 1000
PROXY_STEPS = 900

ProgressFn = Callable[[float, int], None]  # (completed steps, total steps)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(asset_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(asset_id, threading.Lock())


class NoVideoError(Exception):
    """The asset has no video stream, so there is nothing to proxy."""


# --- what exists -------------------------------------------------------------------------


def proxy_id(media: MediaStore, original_id: str) -> str:
    return media.derived_id(original_id, PROXY_SUFFIX)


def thumbnails_id(media: MediaStore, original_id: str) -> str:
    return media.derived_id(original_id, THUMBS_SUFFIX)


def _current(media: MediaStore, asset: assets.Asset, name: str, derived: str) -> dict[str, Any] | None:
    """The cache entry for `name` if it is for this asset and version and its file is as saved."""
    payload = media.load_cache(asset.path, name)
    if not isinstance(payload, dict) or payload.get("version") != VERSION or payload.get("asset") != asset.id:
        return None
    try:
        if media.stat(derived).size != payload.get("bytes"):
            return None
    except MediaError:
        return None
    return payload


def proxy_info(media: MediaStore, asset: assets.Asset) -> dict[str, Any] | None:
    """The proxy's metadata (`width`, `height`, `fps`, `bytes`, `duration`) if a current one exists."""
    return _current(media, asset, PROXY_NAME, proxy_id(media, asset.path))


def thumbnails_info(media: MediaStore, asset: assets.Asset) -> dict[str, Any] | None:
    """The strip's metadata (`tile_width`, `tile_height`, `columns`, `rows`, `count`, `interval`,
    `width`, `height`, `duration`) if a current one exists."""
    return _current(media, asset, THUMBS_NAME, thumbnails_id(media, asset.path))


def flags(media: MediaStore, asset: assets.Asset) -> dict[str, bool]:
    """What an asset summary says about its derived files."""
    if not asset.has_video:
        return {"proxy": False, "thumbnails": False}
    return {"proxy": proxy_info(media, asset) is not None, "thumbnails": thumbnails_info(media, asset) is not None}


def visual_source(media: MediaStore, asset_id: str) -> str:
    """The media id to read for visual work on an asset: its proxy if a current one exists, else the
    original. Never raises for a missing proxy (`AssetNotFoundError` / `SourceNotFoundError` for a
    missing asset, as `assets.get_asset`)."""
    asset = assets.get_asset(media, asset_id)
    if asset.has_video and proxy_info(media, asset) is not None:
        return proxy_id(media, asset.path)
    return asset.path


def pending(media: MediaStore) -> list[str]:
    """Ids of the library's present video assets that lack a current proxy or strip."""
    with assets.open_book(media) as book:
        rows = [a for a in book.list() if a.has_video and book.present(a)]
    return [a.id for a in rows if not all(flags(media, a).values())]


# --- making them -------------------------------------------------------------------------


@dataclass(frozen=True)
class Strip:
    tile_width: int
    tile_height: int
    columns: int
    rows: int
    count: int
    interval: float


def plan_strip(duration: float, width: int, height: int) -> Strip:
    """The tile grid for a video of `duration` seconds (see the module docstring)."""
    interval = max(MIN_INTERVAL, duration / MAX_TILES)
    count = max(1, min(MAX_TILES, math.ceil(duration / interval)))
    tile_height = max(2, round(TILE_WIDTH * height / width / 2) * 2)
    columns = min(COLUMNS, count)
    return Strip(TILE_WIDTH, tile_height, columns, math.ceil(count / columns), count, round(interval, 3))


def _proxy_command(ffmpeg: str, source: str, out: str, fps: float | None, has_audio: bool) -> list[str]:
    scale = f"scale=trunc(min({PROXY_MAX_WIDTH}\\,iw)/2)*2:-2"
    vf = scale if fps is not None and fps <= PROXY_MAX_FPS + 0.5 else f"fps={PROXY_MAX_FPS},{scale}"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", source, "-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "0:a:0"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(PROXY_CRF), "-pix_fmt", "yuv420p"]
    cmd += ["-g", str(PROXY_KEYFRAME_FRAMES), "-keyint_min", str(PROXY_KEYFRAME_FRAMES), "-sc_threshold", "0"]
    cmd += ["-c:a", "aac", "-ac", "1", "-b:a", "64k"] if has_audio else ["-an"]
    return [*cmd, "-movflags", "+faststart", "-f", "mp4", out]


def _strip_command(ffmpeg: str, source: str, out: str, strip: Strip) -> list[str]:
    vf = f"fps=1/{strip.interval:g}:round=up,scale={strip.tile_width}:{strip.tile_height},tile={strip.columns}x{strip.rows}"
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-skip_frame", "nokey", "-i", source,
        "-an", "-vf", vf, "-frames:v", "1", "-q:v", "5", "-update", "1", out,
    ]  # fmt: skip


def _run(cmd: list[str], on_seconds: Callable[[float], None], cancel: CancelToken | None, what: str) -> None:
    result = run_with_progress(cmd, on_seconds, cancel)
    if result.returncode != 0:
        raise ExtractError(f"ffmpeg failed to make the {what}: {result.stderr.strip()[-2000:]}")


def _make(media: MediaStore, derived: str, build: Callable[[str, str], None]) -> int:
    """Run `build(path, derived path)` writing `derived`; delete it on any failure. Returns its size."""
    try:
        with media.write_local(derived) as out:
            build(str(out), derived)
        return media.stat(derived).size
    except BaseException:
        media.delete(derived)
        raise


def prepare(
    media: MediaStore,
    asset_id: str,
    *,
    force: bool = False,
    on_progress: ProgressFn | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """Make the asset's proxy and thumbnail strip, whichever is missing or stale (both with `force`).

    Returns `{"asset", "proxy", "thumbnails", "made"}`: the two metadata dicts (None for an
    audio-only asset) and `made`, which of "proxy" and "thumbnails" this call produced (empty when
    everything was current). Raises `AssetNotFoundError` / `SourceNotFoundError` for a missing
    asset, `ExtractError` if ffmpeg fails and `JobCancelledError` through `cancel`, leaving no
    partial file."""
    asset = assets.get_asset(media, asset_id)
    if not asset.has_video:
        return {"asset": asset.id, "proxy": None, "thumbnails": None, "made": [], "skipped": "audio-only asset"}
    report: ProgressFn = on_progress or (lambda done, total: None)
    ffmpeg = _require_binary("ffmpeg")
    made: list[str] = []
    with _lock_for(asset.id):  # a second job for the same asset waits, then finds it current
        proxy_meta = None if force else proxy_info(media, asset)
        if proxy_meta is None:
            proxy_meta = _make_proxy(media, asset, ffmpeg, report, cancel)
            made.append(PROXY_NAME)
        report(PROXY_STEPS, STEPS)
        thumbs_meta = None if force else thumbnails_info(media, asset)
        if thumbs_meta is None:
            thumbs_meta = _make_strip(media, asset, ffmpeg, report, cancel)
            made.append(THUMBS_NAME)
        report(STEPS, STEPS)
    return {"asset": asset.id, "proxy": proxy_meta, "thumbnails": thumbs_meta, "made": made}


def _make_proxy(
    media: MediaStore, asset: assets.Asset, ffmpeg: str, report: ProgressFn, cancel: CancelToken | None
) -> dict[str, Any]:
    derived = proxy_id(media, asset.path)
    total = max(asset.duration, 0.001)

    def build(out: str, _: str) -> None:
        with media.local_path(asset.path) as source:
            cmd = _proxy_command(ffmpeg, str(source), out, asset.fps, asset.has_audio)
            _run(cmd, lambda s: report(min(1.0, s / total) * PROXY_STEPS, STEPS), cancel, "proxy")

    size = _make(media, derived, build)
    width = int(min(PROXY_MAX_WIDTH, asset.width or PROXY_MAX_WIDTH) // 2 * 2)
    meta = {
        "version": VERSION,
        "asset": asset.id,
        "bytes": size,
        "duration": asset.duration,
        "width": width,
        "height": max(2, round((asset.height or 0) * width / (asset.width or 1) / 2) * 2),
        "fps": min(asset.fps, PROXY_MAX_FPS) if asset.fps else PROXY_MAX_FPS,
        "has_audio": asset.has_audio,
    }
    media.save_cache(asset.path, PROXY_NAME, meta)
    return meta


def _make_strip(
    media: MediaStore, asset: assets.Asset, ffmpeg: str, report: ProgressFn, cancel: CancelToken | None
) -> dict[str, Any]:
    derived = thumbnails_id(media, asset.path)
    proxy_meta = proxy_info(media, asset)
    source_id = proxy_id(media, asset.path) if proxy_meta is not None else asset.path
    strip = plan_strip(asset.duration, asset.width or 1, asset.height or 1)
    total = max(asset.duration, 0.001)

    def build(out: str, _: str) -> None:
        with media.local_path(source_id) as source:
            cmd = _strip_command(ffmpeg, str(source), out, strip)
            _run(
                cmd,
                lambda s: report(PROXY_STEPS + min(1.0, s / total) * (STEPS - PROXY_STEPS), STEPS),
                cancel,
                "thumbnail strip",
            )

    size = _make(media, derived, build)
    meta = {
        "version": VERSION,
        "asset": asset.id,
        "bytes": size,
        "duration": asset.duration,
        "tile_width": strip.tile_width,
        "tile_height": strip.tile_height,
        "columns": strip.columns,
        "rows": strip.rows,
        "count": strip.count,
        "interval": strip.interval,
        "width": strip.tile_width * strip.columns,
        "height": strip.tile_height * strip.rows,
        "source": "proxy" if proxy_meta is not None else "original",
    }
    media.save_cache(asset.path, THUMBS_NAME, meta)
    return meta
