"""Scene changes and a screen-activity curve, from one cheap ffmpeg pass over the picture, stored with the asset.

For a coding stream or a lecture the interesting moments show on the screen, not in the audio: a window
switch, a new slide, a burst of typing. Every video asset can have one `scenes` document with two signals
that later cards (highlights, contact sheets, captions) read through `load`, `scenes_between` and
`activity_between`.

The pass
--------
One ffmpeg run reads `proxy.visual_source(media, asset_id)` (the proxy if it is current, else the original;
the document says which in `source`) and decodes it into a tiny picture: `fps=<SAMPLE_FPS>` (5, or the
source's own rate if that is lower, so a slideshow is not padded with repeats), `scale=160:-2` (area
averaging, so thin text and a cursor still register) and `format=gray`. ffmpeg's `scdet` filter then
measures every sampled frame against the one before it and `metadata=print` writes its numbers to stdout,
which is parsed line by line as it arrives (memory does not grow with the length of the file; a 3-hour
proxy is 54,000 lines). Nothing is encoded.

`scdet` gives two numbers per frame: `mafd`, the mean absolute difference from the previous frame in 0..255
grey levels (0 for an identical frame), and `score`, `min(mafd, |mafd - previous mafd|)`: that is large only
for a frame that differs a lot from the one before it while the one before it differed little from ITS
predecessor, i.e. a cut, not a continuous pan, a scroll or a fade (a slow dissolve is therefore not found).

Scene changes
-------------
A sampled frame with `score / 100 >= threshold` is a cut, at that frame's time (the first frame of the new
shot; accurate to one sample period, 0.2 s at 5 fps). `threshold` is 0..1, as in ffmpeg's
`select='gt(scene,X)'`, default `DEFAULT_THRESHOLD` = 0.15 (a typed line or a scroll scores well under 0.05; a
window switch between two editors 0.2 to 0.5; a camera cut more). Cuts closer together than `min_gap`
seconds (default 1.0) are one: the strongest of the burst is kept, which stops a flickering transition from
filling the list. Each cut has its `score` (0..1, rounded to 3 decimals).

Activity
--------
One value per `ACTIVITY_INTERVAL` (1 s) bucket, starting at 0: the mean `mafd` of the frames in the bucket
(0 for a bucket with none), mapped to 0..1 by `log1p(10 * mafd) / log1p(500)` and clipped, which is
logarithmic on purpose: the same curve has to separate "nothing changed" (0.0), "someone typing in an
otherwise still editor" (about 0.05 to 0.2), "something is moving" (0.2 to 0.5) and "the whole picture
changed" (a mean `mafd` of 50 or more reads 1.0). A bucket with a cut in it reads about 0.8 or more at 5
fps. Values have 3 decimals; a 3-hour file is about 10,800 of them.

The document
------------
Stored through `MediaStore.save_cache(<the asset's media id>, "scenes", ...)`, so it is keyed by the
original's size and mtime and is stale when the file changes (`VERSION` is bumped if the shape changes):

    {"version": 1,
     "asset": "a_0123456789abcdef",
     "source": "proxy",              # "proxy" or "original": what the pass read
     "duration": 26.0,               # seconds of the asset
     "threshold": 0.15, "min_gap": 1.0,
     "sample_fps": 5.0,
     "activity_interval": 1.0,       # seconds per activity value
     "scenes": [{"time": 5.0, "score": 0.655}, ...],
     "activity": [0.141, 0.0, ...]}  # activity[k] covers [k * interval, (k + 1) * interval)

plus a small second entry "scenes_info" (`{version, asset, source, duration, threshold, min_gap, scenes,
activity_buckets}`, the counts) so listings can say whether an asset has the signals without reading them.

`analyze` is idempotent: with a current document made with the same parameters from the same kind of source
it does nothing (so making a proxy afterwards makes the next run redo it from the proxy). An audio-only
asset has no picture and is reported as skipped.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable
from typing import Any

from split_video import assets, proxy
from split_video.ffmpeg import _require_binary, iter_stdout_lines
from split_video.jobs import CancelToken
from split_video.media_store import MediaStore

VERSION = 1
CACHE_NAME = "scenes"
INFO_NAME = "scenes_info"

DEFAULT_THRESHOLD = 0.15
DEFAULT_MIN_GAP = 1.0
ACTIVITY_INTERVAL = 1.0
SAMPLE_FPS = 5.0
SAMPLE_WIDTH = 160
# A bucket whose mean frame difference is this many grey levels (of 255) or more reads 1.0.
FULL_ACTIVITY_MAFD = 50.0
_LOG_GAIN = 10.0

AUDIO_ONLY = "audio-only asset"

ProgressFn = Callable[[float, float], None]  # (seconds of video done, seconds in all)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(asset_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(asset_id, threading.Lock())


def activity_of(mafd: float) -> float:
    """The 0..1 activity for a mean absolute frame difference (grey levels, 0..255)."""
    return min(1.0, math.log1p(_LOG_GAIN * max(0.0, mafd)) / math.log1p(_LOG_GAIN * FULL_ACTIVITY_MAFD))


# --- parsing ffmpeg's output ---------------------------------------------------------------------


class SignalParser:
    """Folds `metadata=print` lines into cuts and activity buckets, keeping only those.

    A frame prints as `frame:25 pts:125 pts_time:5` followed by `lavfi.scd.mafd=...` and
    `lavfi.scd.score=...` (the latter ends the frame). `seconds` is the time of the last frame seen."""

    def __init__(self, threshold: float, min_gap: float, interval: float = ACTIVITY_INTERVAL) -> None:
        self.threshold = threshold
        self.min_gap = min_gap
        self.interval = interval
        self.scenes: list[dict[str, float]] = []
        self.seconds = 0.0
        self._sums: list[float] = []
        self._counts: list[int] = []
        self._time: float | None = None
        self._mafd: float | None = None

    def feed(self, line: str) -> None:
        line = line.strip()
        if line.startswith("frame:"):
            self._time, self._mafd = None, None
            for part in line.split():
                if part.startswith("pts_time:"):
                    self._time = _number(part[9:])
        elif line.startswith("lavfi.scd.mafd="):
            self._mafd = _number(line[15:])
        elif line.startswith("lavfi.scd.score="):
            score = _number(line[16:])
            if self._time is not None and self._mafd is not None and score is not None:
                self._frame(self._time, self._mafd, score / 100.0)
            self._time, self._mafd = None, None

    def _frame(self, time: float, mafd: float, score: float) -> None:
        self.seconds = max(self.seconds, time)
        bucket = max(0, int(time / self.interval))
        while len(self._sums) <= bucket:
            self._sums.append(0.0)
            self._counts.append(0)
        self._sums[bucket] += mafd
        self._counts[bucket] += 1
        if score >= self.threshold:
            self._cut(time, score)

    def _cut(self, time: float, score: float) -> None:
        if self.scenes and time - self.scenes[-1]["time"] < self.min_gap:
            if score > self.scenes[-1]["score"]:  # the strongest of a burst stands for it
                self.scenes[-1] = {"time": time, "score": score}
        else:
            self.scenes.append({"time": time, "score": score})

    def result(self, duration: float) -> tuple[list[dict[str, float]], list[float]]:
        """`(scenes, activity)`: the activity has one value per bucket up to `duration`."""
        buckets = max(1, math.ceil(duration / self.interval - 1e-9))
        activity = [
            round(activity_of(self._sums[k] / self._counts[k]), 3) if k < len(self._sums) and self._counts[k] else 0.0
            for k in range(buckets)
        ]
        scenes = [{"time": round(s["time"], 3), "score": round(min(1.0, s["score"]), 3)} for s in self.scenes]
        return scenes, activity


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def scan_lines(
    lines: Iterable[str],
    parser: SignalParser,
    total: float,
    on_progress: ProgressFn | None = None,
    cancel: CancelToken | None = None,
) -> None:
    """Feed `lines` to `parser`, reporting `(seconds done, total)` about every second of video."""
    reported = -1.0
    for line in lines:
        if cancel is not None:
            cancel.raise_if_cancelled()
        parser.feed(line)
        if on_progress is not None and parser.seconds - reported >= 1.0:
            reported = parser.seconds
            on_progress(min(parser.seconds, total), total)


# --- the pass ----------------------------------------------------------------------------------


def _command(ffmpeg: str, source: str, fps: float) -> list[str]:
    vf = f"fps={fps:g},scale={SAMPLE_WIDTH}:-2:flags=area,format=gray,scdet=t=100,metadata=mode=print:file=-"
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source,
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        vf,
        "-f",
        "null",
        "-",
    ]


def _valid(payload: Any, asset: assets.Asset) -> bool:
    return isinstance(payload, dict) and payload.get("version") == VERSION and payload.get("asset") == asset.id


def load(media: MediaStore, asset_id: str) -> dict[str, Any] | None:
    """The asset's stored signals (the document in the module docstring), or None if it has none or the file has
    changed since. `AssetNotFoundError` / `SourceNotFoundError` for a missing asset."""
    asset = assets.get_asset(media, asset_id)
    payload = media.load_cache(asset.path, CACHE_NAME)
    return payload if _valid(payload, asset) else None


def info(media: MediaStore, asset: assets.Asset) -> dict[str, Any] | None:
    """The counts entry for a current document, else None. Cheap."""
    payload = media.load_cache(asset.path, INFO_NAME)
    return payload if _valid(payload, asset) else None


def has_scenes(media: MediaStore, asset: assets.Asset) -> bool:
    """What an asset summary says (`scenes`)."""
    return info(media, asset) is not None


def scenes_between(
    document: dict[str, Any], start: float | None = None, end: float | None = None
) -> list[dict[str, float]]:
    """The cuts with `start <= time < end` (either bound may be None)."""
    return [s for s in document["scenes"] if (start is None or s["time"] >= start) and (end is None or s["time"] < end)]


def activity_between(document: dict[str, Any], start: float | None = None, end: float | None = None) -> list[float]:
    """The activity values of the buckets that overlap `[start, end)`, in order (the first one covers
    `floor(start / activity_interval) * activity_interval`)."""
    values = document["activity"]
    interval = document["activity_interval"]
    first = 0 if start is None else max(0, int(start // interval))
    last = len(values) if end is None else min(len(values), math.ceil(end / interval - 1e-9))
    return values[first:last]


def excerpt(document: dict[str, Any], start: float | None = None, end: float | None = None) -> dict[str, Any]:
    """The document cut to `[start, end)`: its cuts in that span and the activity values that overlap it, with
    `activity_start` (the second the first value covers). Whole when neither bound is given."""
    if start is None and end is None:
        return document
    interval = document["activity_interval"]
    first = 0 if start is None else max(0, int(start // interval))
    return {
        **document,
        "scenes": scenes_between(document, start, end),
        "activity": activity_between(document, start, end),
        "activity_start": round(first * interval, 3),
    }


def analyze(
    media: MediaStore,
    asset_id: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_gap: float = DEFAULT_MIN_GAP,
    force: bool = False,
    on_progress: ProgressFn | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """Find the asset's scene changes and activity curve and store them, unless a current document made with the
    same `threshold` and `min_gap` from the same kind of source exists (`force` redoes it). Returns the info
    `{asset, source, duration, threshold, min_gap, scenes, activity_buckets}` (counts) plus `made`; an audio-only
    asset gives `{asset, skipped: "audio-only asset", made: False}`.

    Raises `ValueError` for a threshold outside 0..1 or a negative gap, `ExtractError` if ffmpeg fails,
    `AssetNotFoundError` / `SourceNotFoundError` for a missing asset and `JobCancelledError` through `cancel`; a
    failed or cancelled run stores nothing and leaves any earlier document alone."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    if min_gap < 0:
        raise ValueError("min_gap must not be negative")
    asset = assets.get_asset(media, asset_id)
    if not asset.has_video:
        return {"asset": asset.id, "skipped": AUDIO_ONLY, "made": False}
    ffmpeg = _require_binary("ffmpeg")
    with _lock_for(asset.id):  # a second job for the same asset waits, then finds the document current
        source_id = proxy.visual_source(media, asset.id)
        kind = "original" if source_id == asset.path else "proxy"
        current = None if force else info(media, asset)
        if (
            current is not None
            and current["source"] == kind
            and current["threshold"] == threshold
            and current["min_gap"] == min_gap
        ):
            return {**current, "made": False}
        total = max(asset.duration, 0.001)
        fps = min(SAMPLE_FPS, asset.fps) if asset.fps else SAMPLE_FPS
        parser = SignalParser(threshold, min_gap)
        if on_progress is not None:
            on_progress(0.0, total)
        with media.local_path(source_id) as source:
            lines = iter_stdout_lines(_command(ffmpeg, str(source), fps), cancel, "analyse the scenes")
            try:
                scan_lines(lines, parser, total, on_progress, cancel)
            finally:
                lines.close()
        scenes, activity = parser.result(asset.duration)
        document = {
            "version": VERSION,
            "asset": asset.id,
            "source": kind,
            "duration": asset.duration,
            "threshold": threshold,
            "min_gap": min_gap,
            "sample_fps": fps,
            "activity_interval": ACTIVITY_INTERVAL,
            "scenes": scenes,
            "activity": activity,
        }
        summary = {
            "version": VERSION,
            "asset": asset.id,
            "source": kind,
            "duration": asset.duration,
            "threshold": threshold,
            "min_gap": min_gap,
            "scenes": len(scenes),
            "activity_buckets": len(activity),
        }
        media.save_cache(asset.path, CACHE_NAME, document)
        media.save_cache(asset.path, INFO_NAME, summary)
        if on_progress is not None:
            on_progress(total, total)
        return {**summary, "made": True}
