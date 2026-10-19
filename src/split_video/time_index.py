"""What is happening at a timestamp: every stored signal of an asset around one time, in one call (SPL-100).

`what_is_happening(media, asset, time, window)` answers "what is going on at 1:23:45?" from what the other tools
have already stored: the audio class (YAMNet), the loudness, the silences, the words spoken, the scene cuts and screen
activity, and the on-screen text. Nothing is decoded and no model runs: an agent that asks gets the answer in
milliseconds, and a signal that was never computed is `null` with a hint saying which command makes it.

For many calls (a media report walking a 3 hour recording) load the documents once and slice them:

    index = TimeIndex.load(media, asset_id)   # reads the stored documents, once
    index.at(time, window)                    # pure and fast: centred on `time`
    index.window(start, end)                  # the same shape for an explicit span
    index.timeline(start, end, step)          # compact windows, one per `step` seconds

Arguments
---------
`time` is the centre, in seconds from the start (0 to the asset's duration). `window` is the total span in seconds
(default `DEFAULT_WINDOW` = 10, above 0 and at most `MAX_WINDOW` = 600) centred on `time` and clamped to
`[0, duration]`, so at `time` 0 the window is `[0, window / 2]`. An unknown asset raises `AssetNotFoundError`; a
`time` that is negative, not a number or beyond the duration, or a bad `window`, raises `TimeIndexError`. Nothing
else fails: a missing signal is reported, not raised.

The result (version 1, stable)
------------------------------
Keys are never added or removed within a version; a signal that has not been computed is `null`, never absent.
Times are seconds, rounded to 3 decimals; levels are dBFS.

    {"version": 1, "asset": "a_...", "time": 205.0, "window": {"start": 200.0, "end": 210.0}, "duration": 3600.0,
     "audio": {                      # null without the audio classification
        "class": "applause_crowd",   # the class with the most seconds in the window (null if none covers it):
                                     # music, singing, speech, applause_crowd, laughter or silence_other
        "share": 1.0,                # that class's fraction of the window
        "secondary": null,           # the class with the second most seconds, or null
        "regions": [{"start", "end", "class", "score"}],  # clipped to the window, at most 100
        "regions_truncated": false},
     "loudness": {                   # null without a stored loudness curve
        "mean_db", "max_db", "min_db",  # over the 1 s RMS values that overlap the window (mean of the dB values)
        "window_seconds": 1.0,
        "curve": [{"t", "db"}]},     # at most 60 points; thinned by keeping the loudest of each group
     "silence": {                    # null without a stored silence scan (made by `propose`)
        "threshold_db": -35.0,       # the scan used: -35 dB if stored, else the closest stored one
        "inside": false,             # `time` is inside a silence
        "intervals": [{"start", "end"}],  # silences of at least 0.5 s, clipped to the window, at most 50
        "truncated": false},
     "speech": {                     # null without a transcript
        "words": [{"start", "end", "word"}],  # the words overlapping the window, at most 200
        "text": "the words joined",  # of the listed words
        "word_count": 31,            # all words in the window, not only those listed
        "words_per_minute": 186.0,   # word_count / window length
        "truncated": false},
     "visual": {                     # always an object; each member is null when its signal is missing
        "scene_change": {"time", "score"},  # the cut nearest `time` within the window (scenes), or null
        "scene_changes": 1,          # cuts inside the window
        "scene_index": 3,            # cuts at or before `time` (0: before the first cut)
        "shot_start": 150.0,         # the time of the last cut at or before `time` (0.0 before the first)
        "activity": {"mean", "max", "curve": [{"t", "value"}]},  # 0..1 per second; at most 60 points
        "description": null,         # reserved for picture descriptions (SPL-97); always null for now
        "text": {                    # OCR: the text that was on screen at `time` (else the latest text shown in the
                                     # window), or null when there is none
           "time", "until",          # when that text appeared and when it was replaced
           "text", "lines",          # at most 1000 characters / 30 lines
           "truncated", "more"}},    # `more`: other text states shown in the window
     "available": {"audio", "loudness", "silence", "speech", "scenes", "ocr", "description"},  # booleans
     "missing": [{"signal": "speech", "how": "run `split-video transcribe <target>` ..."}]}

`available.description` is always false and is not listed in `missing` (nothing makes it yet). A signal the asset
cannot have (no audio track: audio, loudness, silence, speech; no picture: scenes, ocr) is neither available nor
listed in `missing`. A default (10 s) response is 1.5 to 3 KB as compact JSON; the widest possible one (600 s window of a dense recording)
is about 15 KB (see docs/time-index.md).

Where it comes from: `classification` (`ClassificationCache`, regions at the default thresholds), `waveform`
(`WaveformCache.stored_loudness`: a missing curve is NOT computed here, that would decode the whole audio; open
the video in the editor to make it), `silence` (`SilenceCache.stored`), the transcript, `scenes` and `ocr` documents.

Timeline
--------
`timeline(start, end, step)` returns `{"version": 1, "asset", "duration", "start", "end", "step", "count", "points",
"available", "missing"}`: one compact point per `step` seconds from `start` to `end` (defaults: the whole asset; step
60 s, raised so there are at most `MAX_POINTS` = 500 points; an explicit step that would give more is an error).
A point is the window `[t, t + step)` clamped to `end`:

    {"start", "end", "audio": class|null, "loudness_db": mean|null, "loudness_max_db": max|null,
     "word_count": n|null, "words_per_minute": x|null, "scene_changes": n|null, "activity": mean|null,
     "text": first line of the on-screen text|null (at most 120 characters)}

Same values as the matching `window(t, t + step)` call, minus the lists.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from typing import Any

from split_video import assets, ocr, scenes, transcribe
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.media_store import MediaStore

VERSION = 1
DEFAULT_WINDOW = 10.0
MAX_WINDOW = 600.0
MAX_WORDS = 200
MAX_CURVE = 60
MAX_REGIONS = 100
MAX_SILENCES = 50
MAX_TEXT_CHARS = 1000
MAX_TEXT_LINES = 30
TIMELINE_TEXT_CHARS = 120
MIN_SILENCE = 0.5
SILENCE_THRESHOLD_DB = -35.0
MAX_POINTS = 500
DEFAULT_STEP = 60.0
MIN_STEP = 1.0
_EPS = 1e-9

SIGNALS = ("audio", "loudness", "silence", "speech", "scenes", "ocr")

HOW = {
    "audio": (
        "run `split-video analyze <video>` (CLI), the analyze_audio tool (MCP) or POST /api/v1/analyze in the editor "
        "(HTTP)"
    ),
    "loudness": "open the video in the editor (`split-video edit`): GET /api/v1/waveform stores the loudness curve",
    "silence": "run `split-video propose <video>` (CLI), the propose_splits tool (MCP) or POST /api/v1/detect (HTTP)",
    "speech": (
        "run `split-video transcribe <target>` (CLI), the transcribe_asset tool (MCP) or "
        "POST /api/v1/assets/{id}/transcribe (HTTP)"
    ),
    "scenes": (
        "run `split-video scenes <target>` (CLI), the analyze_scenes tool (MCP) or "
        "POST /api/v1/assets/{id}/scenes (HTTP)"
    ),
    "ocr": ("run `split-video ocr <target>` (CLI), the ocr_asset tool (MCP) or POST /api/v1/assets/{id}/ocr (HTTP)"),
}
_NEEDS_AUDIO = ("audio", "loudness", "silence", "speech")
_NEEDS_VIDEO = ("scenes", "ocr")
_CLASS_ORDER = {bucket: i for i, bucket in enumerate(classify.BUCKET_LABELS)}


class TimeIndexError(ValueError):
    """A bad `time`, `window`, span or `step`. The message says what is allowed."""


def _r(value: float) -> float:
    return round(value, 3)


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise TimeIndexError(f"{name} must be a number of seconds")
    return float(value)


def _thin(points: list[tuple[float, float]], cap: int = MAX_CURVE) -> list[tuple[float, float]]:
    """At most `cap` points: groups of consecutive points are replaced by their loudest/highest one, at the time of
    the group's first point."""
    if len(points) <= cap:
        return points
    size = math.ceil(len(points) / cap)
    return [(points[i][0], max(v for _, v in points[i : i + size])) for i in range(0, len(points), size)]


def _clipped_overlaps(starts: list[float], ends: list[float], lo: float, hi: float) -> range:
    """Indexes of the sorted, non-overlapping spans that overlap `[lo, hi)`."""
    return range(bisect_right(ends, lo), bisect_left(starts, hi))


class TimeIndex:
    """The stored signals of one asset, ready to be sliced. Build with `load`; every method is pure."""

    def __init__(
        self,
        asset: assets.Asset,
        *,
        regions: list[classify.ClassificationRegion] | None = None,
        loudness: Any = None,
        silences: tuple[float, list[Any]] | None = None,
        transcript: dict[str, Any] | None = None,
        scene_document: dict[str, Any] | None = None,
        ocr_document: dict[str, Any] | None = None,
    ) -> None:
        self.asset = asset
        self.duration = float(asset.duration)
        # audio classification
        self._regions = regions
        self._r_starts = [r.start for r in regions or []]
        self._r_ends = [r.end for r in regions or []]
        # loudness
        self._loudness = loudness
        # silences
        self._silence_db: float | None = None
        self._s_starts: list[float] = []
        self._s_ends: list[float] = []
        self._silence_on = silences is not None
        if silences is not None:
            self._silence_db = silences[0]
            kept = sorted((i.start, i.end) for i in silences[1] if i.end - i.start >= MIN_SILENCE)
            self._s_starts = [s for s, _ in kept]
            self._s_ends = [e for _, e in kept]
        # speech
        self._words: list[dict[str, Any]] | None = None
        if transcript is not None:
            flat = [w for s in transcript["segments"] for w in s["words"]]
            flat.sort(key=lambda w: (w["start"], w["end"]))
            self._words = [{"start": w["start"], "end": w["end"], "word": w["word"]} for w in flat]
            self._w_starts = [w["start"] for w in self._words]
            top, self._w_ends = -math.inf, []
            for w in self._words:  # running maximum: monotone, so a bisect finds the first word still sounding
                top = max(top, w["end"])
                self._w_ends.append(top)
        # scenes
        self._scenes = scene_document
        self._cuts = [s["time"] for s in scene_document["scenes"]] if scene_document else []
        # on-screen text
        self._ocr_on = ocr_document is not None
        self._states = sorted(ocr.states(ocr_document), key=lambda s: s["time"]) if ocr_document else []
        self._o_starts = [s["time"] for s in self._states]

    @classmethod
    def load(cls, media: MediaStore, asset_id: str) -> TimeIndex:
        """Read every stored signal of the asset (`AssetNotFoundError` for an unknown one). Reads documents only."""
        asset = assets.get_asset(media, asset_id)
        classification = ClassificationCache(media, asset.path)
        stored = SilenceCache(media, asset.path).stored()
        silences = None
        if stored:
            threshold = min(stored, key=lambda t: (abs(t - SILENCE_THRESHOLD_DB), t))
            silences = (threshold, stored[threshold])
        return cls(
            asset,
            regions=classification.get_regions() if classification.is_analyzed else None,
            loudness=WaveformCache(media, asset.path).stored_loudness(),
            silences=silences,
            transcript=transcribe.load(media, asset.id),
            scene_document=scenes.load(media, asset.id),
            ocr_document=ocr.load(media, asset.id),
        )

    # --- what is there ------------------------------------------------------------------------

    @property
    def available(self) -> dict[str, bool]:
        return {
            "audio": self._regions is not None,
            "loudness": self._loudness is not None,
            "silence": self._silence_on,
            "speech": self._words is not None,
            "scenes": self._scenes is not None,
            "ocr": self._ocr_on,
            "description": False,
        }

    @property
    def missing(self) -> list[dict[str, str]]:
        have = self.available
        impossible = ()
        if not self.asset.has_audio:
            impossible += _NEEDS_AUDIO
        if not self.asset.has_video:
            impossible += _NEEDS_VIDEO
        return [{"signal": s, "how": HOW[s]} for s in SIGNALS if not have[s] and s not in impossible]

    # --- asking -------------------------------------------------------------------------------

    def at(self, time: float, window: float | None = None) -> dict[str, Any]:
        """Everything around `time`, in a window of `window` seconds centred on it (see the module docstring)."""
        time = _number("time", time)
        width = DEFAULT_WINDOW if window is None else _number("window", window)
        if time < 0:
            raise TimeIndexError(f"time must not be negative (got {time:g})")
        if time > self.duration + _EPS:
            raise TimeIndexError(f"time {time:g} is beyond the end of the asset ({self.duration:g} s)")
        if not 0 < width <= MAX_WINDOW:
            raise TimeIndexError(f"window must be above 0 and at most {MAX_WINDOW:g} seconds (got {width:g})")
        start, end = max(0.0, time - width / 2), min(self.duration, time + width / 2)
        return self._build(min(time, self.duration), start, end)

    def window(self, start: float, end: float) -> dict[str, Any]:
        """The same answer for the explicit span `[start, end]`; `time` is its centre."""
        start, end = _number("start", start), _number("end", end)
        self._check_span(start, end)
        return self._build((start + end) / 2, start, end)

    def timeline(
        self, start: float | None = None, end: float | None = None, step: float | None = None
    ) -> dict[str, Any]:
        """Compact points, one per `step` seconds between `start` and `end` (see the module docstring)."""
        lo = 0.0 if start is None else _number("start", start)
        hi = self.duration if end is None else _number("end", end)
        self._check_span(lo, hi, limit=math.inf)
        if step is None:
            size = max(DEFAULT_STEP, math.ceil((hi - lo) / MAX_POINTS))
        else:
            size = _number("step", step)
            if size < MIN_STEP:
                raise TimeIndexError(f"step must be at least {MIN_STEP:g} second")
        count = math.ceil((hi - lo) / size - _EPS)
        if count > MAX_POINTS:
            raise TimeIndexError(
                f"{count} points is too many (at most {MAX_POINTS}): use a step of at least "
                f"{math.ceil((hi - lo) / MAX_POINTS)} seconds or a shorter span"
            )
        points = []
        for k in range(count):
            a = lo + k * size
            b = min(hi, a + size)
            full = self._build((a + b) / 2, a, b)
            points.append(_compact(full))
        return {
            "version": VERSION,
            "asset": self.asset.id,
            "duration": _r(self.duration),
            "start": _r(lo),
            "end": _r(hi),
            "step": _r(size),
            "count": len(points),
            "points": points,
            "available": self.available,
            "missing": self.missing,
        }

    # --- building one answer --------------------------------------------------------------------

    def _check_span(self, start: float, end: float, limit: float = MAX_WINDOW) -> None:
        if start < 0:
            raise TimeIndexError(f"start must not be negative (got {start:g})")
        if end > self.duration + _EPS:
            raise TimeIndexError(f"end {end:g} is beyond the end of the asset ({self.duration:g} s)")
        if end <= start:
            raise TimeIndexError(f"end ({end:g}) must be after start ({start:g})")
        if end - start > limit:
            raise TimeIndexError(f"the span must be at most {MAX_WINDOW:g} seconds (got {end - start:g})")

    def _build(self, time: float, start: float, end: float) -> dict[str, Any]:
        return {
            "version": VERSION,
            "asset": self.asset.id,
            "time": _r(time),
            "window": {"start": _r(start), "end": _r(end)},
            "duration": _r(self.duration),
            "audio": self._audio(start, end),
            "loudness": self._loudness_of(start, end),
            "silence": self._silence_of(time, start, end),
            "speech": self._speech(start, end),
            "visual": {
                **self._scene_signals(time, start, end),
                "description": None,
                "text": self._text(time, start, end),
            },
            "available": self.available,
            "missing": self.missing,
        }

    def _audio(self, start: float, end: float) -> dict[str, Any] | None:
        if self._regions is None:
            return None
        seconds: dict[str, float] = {}
        regions = []
        for i in _clipped_overlaps(self._r_starts, self._r_ends, start, end):
            r = self._regions[i]
            a, b = max(r.start, start), min(r.end, end)
            if b <= a:
                continue
            seconds[r.bucket] = seconds.get(r.bucket, 0.0) + (b - a)
            regions.append({"start": _r(a), "end": _r(b), "class": r.bucket, "score": _r(r.score)})
        ranked = sorted(seconds, key=lambda c: (-round(seconds[c], 6), _CLASS_ORDER.get(c, 99)))
        length = max(end - start, _EPS)
        return {
            "class": ranked[0] if ranked else None,
            "share": _r(seconds[ranked[0]] / length) if ranked else 0.0,
            "secondary": ranked[1] if len(ranked) > 1 else None,
            "regions": regions[:MAX_REGIONS],
            "regions_truncated": len(regions) > MAX_REGIONS,
        }

    def _loudness_of(self, start: float, end: float) -> dict[str, Any] | None:
        if self._loudness is None:
            return None
        step, db = self._loudness.window_seconds, self._loudness.db
        first, last = max(0, int(start // step)), min(len(db), math.ceil(end / step - _EPS))
        values = db[first:last]
        if not values:
            return {"mean_db": None, "max_db": None, "min_db": None, "window_seconds": step, "curve": []}
        curve = _thin([(_r((first + k) * step), v) for k, v in enumerate(values)])
        return {
            "mean_db": round(sum(values) / len(values), 2),
            "max_db": max(values),
            "min_db": min(values),
            "window_seconds": step,
            "curve": [{"t": t, "db": v} for t, v in curve],
        }

    def _silence_of(self, time: float, start: float, end: float) -> dict[str, Any] | None:
        if not self._silence_on:
            return None
        found = [
            (self._s_starts[i], self._s_ends[i]) for i in _clipped_overlaps(self._s_starts, self._s_ends, start, end)
        ]
        inside = any(a <= time < b for a, b in found)
        intervals = [{"start": _r(max(a, start)), "end": _r(min(b, end))} for a, b in found]
        return {
            "threshold_db": self._silence_db,
            "inside": inside,
            "intervals": intervals[:MAX_SILENCES],
            "truncated": len(intervals) > MAX_SILENCES,
        }

    def _speech(self, start: float, end: float) -> dict[str, Any] | None:
        if self._words is None:
            return None
        lo = bisect_right(self._w_ends, start)
        hi = bisect_left(self._w_starts, end)
        found = [w for w in self._words[lo:hi] if w["end"] > start and w["start"] < end]
        shown = [dict(w) for w in found[:MAX_WORDS]]  # copies: the caller may change what it gets
        length = max(end - start, _EPS)
        return {
            "words": shown,
            "text": " ".join(w["word"] for w in shown),
            "word_count": len(found),
            "words_per_minute": round(len(found) / length * 60, 1),
            "truncated": len(found) > len(shown),
        }

    def _scene_signals(self, time: float, start: float, end: float) -> dict[str, Any]:
        if self._scenes is None:
            return {
                "scene_change": None,
                "scene_changes": None,
                "scene_index": None,
                "shot_start": None,
                "activity": None,
            }
        cuts = self._cuts
        near = None
        inside = range(bisect_left(cuts, start), bisect_right(cuts, end))
        for i in inside:
            if near is None or abs(cuts[i] - time) < abs(cuts[near] - time):
                near = i
        index = bisect_right(cuts, time)
        step, values = self._scenes["activity_interval"], self._scenes["activity"]
        first, last = max(0, int(start // step)), min(len(values), math.ceil(end / step - _EPS))
        window = values[first:last]
        activity = None
        if window:
            curve = _thin([(_r((first + k) * step), v) for k, v in enumerate(window)])
            activity = {
                "mean": round(sum(window) / len(window), 3),
                "max": max(window),
                "curve": [{"t": t, "value": v} for t, v in curve],
            }
        return {
            "scene_change": None
            if near is None
            else {"time": _r(cuts[near]), "score": self._scenes["scenes"][near]["score"]},
            "scene_changes": len(inside),
            "scene_index": index,
            "shot_start": _r(cuts[index - 1]) if index else 0.0,
            "activity": activity,
        }

    def _text(self, time: float, start: float, end: float) -> dict[str, Any] | None:
        if not self._states:
            return None
        probe = min(time, self.duration - 1e-6) if self.duration > 1e-6 else time
        k = bisect_right(self._o_starts, probe) - 1
        current = self._states[k] if k >= 0 and self._states[k]["until"] > probe else None
        overlapping = [
            s for s in self._states[: bisect_left(self._o_starts, end)] if s["until"] > start and s["time"] < end
        ]
        state = current or (overlapping[-1] if overlapping else None)
        if state is None:
            return None
        text, lines = state["text"], state["lines"]
        cut = len(text) > MAX_TEXT_CHARS or len(lines) > MAX_TEXT_LINES
        return {
            "time": _r(state["time"]),
            "until": _r(state["until"]),
            "text": text[:MAX_TEXT_CHARS],
            "lines": lines[:MAX_TEXT_LINES],
            "truncated": cut,
            "more": max(0, len(overlapping) - (1 if state in overlapping else 0)),
        }


def _compact(full: dict[str, Any]) -> dict[str, Any]:
    audio, loud, speech, visual = full["audio"], full["loudness"], full["speech"], full["visual"]
    text = visual["text"]
    return {
        "start": full["window"]["start"],
        "end": full["window"]["end"],
        "audio": None if audio is None else audio["class"],
        "loudness_db": None if loud is None else loud["mean_db"],
        "loudness_max_db": None if loud is None else loud["max_db"],
        "word_count": None if speech is None else speech["word_count"],
        "words_per_minute": None if speech is None else speech["words_per_minute"],
        "scene_changes": visual["scene_changes"],
        "activity": None if visual["activity"] is None else visual["activity"]["mean"],
        "text": None if text is None else (text["lines"][0] if text["lines"] else text["text"])[:TIMELINE_TEXT_CHARS],
    }


def what_is_happening(media: MediaStore, asset_id: str, time: float, window: float | None = None) -> dict[str, Any]:
    """Everything stored about the asset around `time` (seconds), in a `window`-second span centred on it (default
    10, at most 600). See the module docstring for the result. `AssetNotFoundError` for an unknown asset,
    `TimeIndexError` for a bad time or window. Loads the documents on every call: for many calls build one
    `TimeIndex` and use `at`."""
    return TimeIndex.load(media, asset_id).at(time, window)


def timeline_index(
    media: MediaStore,
    asset_id: str,
    start: float | None = None,
    end: float | None = None,
    step: float | None = None,
) -> dict[str, Any]:
    """Compact points over a span: `TimeIndex.timeline` (see the module docstring)."""
    return TimeIndex.load(media, asset_id).timeline(start, end, step)
