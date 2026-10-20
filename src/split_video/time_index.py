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
Keys are never removed or renamed within a version (new ones may be added, see below); a signal that has not been computed is `null`, never absent.
Times are seconds, rounded to 3 decimals; levels are dBFS.

    {"version": 1, "asset": "a_...", "time": 205.0, "window": {"start": 200.0, "end": 210.0}, "duration": 3600.0,
     "audio": {                      # null without the audio classification
        "class": "applause_crowd",   # the class with the most seconds in the window (null if none covers it):
                                     # music, singing, speech, applause_crowd, laughter, silence_other or other_sound
                                     # (silence_other over loud sound, see `other_sound` below)
        "raw_class": null,           # "silence_other" when `class` is other_sound, else null
        "share": 1.0,                # that class's fraction of the window
        "secondary": null,           # the class with the second most seconds, or null
        "regions": [{"start", "end", "class", "score"}],  # clipped to the window, at most 100; an other_sound one
                                     # also has "raw_class": "silence_other"
        "regions_truncated": false},
     "loudness": {                   # null without a stored loudness curve
        "mean_db", "max_db", "min_db",  # over the 1 s RMS values that overlap the window (mean of the dB values)
        "window_seconds": 1.0,
        "curve": [{"t", "db"}]},     # at most 60 points; thinned by keeping the loudest of each group
     "silence": {                    # null without a stored silence scan (made by `propose`)
        "threshold_db": -35.0,       # the scan used: -35 dB if stored, else the closest stored one
        "inside": false,             # `time` is inside a silence AND no transcribed word is being spoken at `time`
        "inside_exact": false,       # `time` is inside a listed silence interval, whatever the transcript says
        "intervals": [{"start", "end"}],  # silences of at least 0.5 s, clipped to the window, at most 50
        "truncated": false},
     "speech": {                     # null without a transcript
        "words": [{"start", "end", "word"}],  # the words overlapping the window, at most 200
        "text": "the words joined",  # of the listed words
        "word_count": 31,            # all words in the window, not only those listed
        "words_per_minute": 186.0,   # word_count / window length
        "truncated": false},
     "visual": {                     # always an object; each member is null when its signal is missing
        "scene_change": {"time", "score", "kind"},  # the cut nearest `time` within the window, or null
        "scene_changes": 1,          # cuts inside the window
        "scene_index": 3,            # cuts at or before `time` (0: before the first cut)
        "shot_start": 150.0,         # the time of the last cut at or before `time` (0.0 before the first)
        "activity": {"mean", "max", "curve": [{"t", "value"}]},  # 0..1 per second; at most 60 points
        "description": null,         # reserved for picture descriptions (SPL-97); always null for now
        "text": {                    # OCR: the text that was on screen at `time` (else the latest text shown in the
                                     # window), or null when there is none
           "time", "until",          # when that text appeared and when it was replaced
           "age_seconds",            # `time` minus the last OCR frame that saw this text (0 if that frame is later)
           "stale",                  # true: this may no longer be on screen (see below)
           "note",                   # null, or a sentence saying why it is stale
           "text", "lines",          # at most 1000 characters / 30 lines
           "truncated", "more"}},    # `more`: other text states shown in the window
     "available": {"audio", "loudness", "silence", "speech", "scenes", "ocr", "description"},  # booleans
     "missing": [{"signal": "speech", "how": "run `split-video transcribe <target>` ..."}]}

Staleness: OCR reads a frame every `interval` seconds (default 30) and at each scene change, so the text reported
at `time` was read `age_seconds` earlier. `visual.text.stale` is true when that age exceeds `STALE_INTERVALS` (2)
times the OCR document's interval, or when a scene change (either kind, see scenes.py) happened after the frame
and at or before `time`: the picture changed and the text was not read again. `note` then says which. Stale text
is still returned (it is the best known); an agent should treat it as possibly outdated, or run `ocr` with a
shorter `interval`.

Silence: the silence scan and the transcript are separate analyses, so they can disagree by a few tenths of a second
at a speech onset (a silence interval that ends 0.4 s after a word starts). `silence.inside` is false when a
transcribed word is being spoken at `time` (`word.start - SPEECH_ONSET_SLACK <= time < word.end`, the slack
being 0.5 s: word starts run late); `silence.inside_exact` is the raw
test against the intervals. Without a transcript the two are equal and `intervals` are never trimmed.

other_sound: the audio classification has no "loud but none of the five" class: YAMNet files pink noise or a
synthesised tone under `silence_other`, and that must not be described as silence. Where the stored loudness curve
shows a `silence_other` stretch above `LOUD_DB` (-45 dBFS, judged per 1 s value) the class is reported as
`other_sound`, with the raw class kept in `raw_class`; quiet stretches stay `silence_other`. This is applied on
read (the stored classification and classify.py are unchanged); without a loudness curve nothing is relabelled.

Within version 1, keys may be added (never removed or renamed); the two additions above (`inside_exact`, and
`age_seconds` / `stale` / `note` in `visual.text`) are of that kind.

`available.description` is always false and is not listed in `missing` (nothing makes it yet). A signal the asset
cannot have (no audio track: audio, loudness, silence, speech; no picture: scenes, ocr) is neither available nor
listed in `missing`. A default (10 s) response is 1.5 to 3 KB as compact JSON; the widest possible one (600 s window of a dense recording)
is about 15 KB (see docs/time-index.md).

Where it comes from: `classification` (`ClassificationCache`, regions at the default thresholds), `waveform`
(`WaveformCache.stored_loudness`: a missing curve is NOT computed here, that would decode the whole audio; `report
--prepare` (media_report.py) or the editor's waveform route makes it), `silence` (`SilenceCache.stored`), the transcript, `scenes` and `ocr` documents.

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

import itertools
import math
from bisect import bisect_left, bisect_right
from dataclasses import replace
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
# Text read more than this many OCR intervals before `time` is stale (a scene change after the frame makes it stale
# at any age).
STALE_INTERVALS = 2.0
MIN_SILENCE = 0.5
# Transcribed word starts run late by up to a few tenths of a second, so a word counts as sounding this long before
# its stated start when `silence.inside` is decided.
SPEECH_ONSET_SLACK = 0.5
SILENCE_THRESHOLD_DB = -35.0
MAX_POINTS = 500
DEFAULT_STEP = 60.0
MIN_STEP = 1.0
_EPS = 1e-9

# A `silence_other` classification (YAMNet's "none of the five classes") over a stretch that is loud is not silence:
# pink noise, a sine tone, a fan. Where the stored loudness curve has the stretch's 1 s values above this level the
# class is reported as `other_sound` (the raw class stays in `raw_class`). Done here, on read: classify.py and the
# cached classification are untouched.
OTHER_SOUND = "other_sound"
LOUD_DB = -45.0

SIGNALS = ("audio", "loudness", "silence", "speech", "scenes", "ocr")

HOW = {
    "audio": (
        "run `split-video analyze <video>` (CLI), the analyze_audio tool (MCP) or POST /api/v1/analyze in the editor "
        "(HTTP)"
    ),
    "loudness": (
        "run `split-video report <target> --prepare` (CLI), the media_report tool with prepare=true (MCP) or "
        "POST /api/v1/assets/{id}/report/prepare (HTTP); the editor's waveform route stores it too"
    ),
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


def _relabel_loud(regions: list[classify.ClassificationRegion], loudness: Any) -> list[classify.ClassificationRegion]:
    """`silence_other` regions split at the loudness curve's 1 s values: the stretches above `LOUD_DB` become
    `other_sound`, the quiet ones stay as they are. Without a curve nothing changes."""
    if loudness is None or not loudness.db:
        return regions
    step, db = loudness.window_seconds, loudness.db
    out: list[classify.ClassificationRegion] = []
    for r in regions:
        if r.bucket != classify.SILENCE_BUCKET:
            out.append(r)
            continue
        cuts = [r.start]
        k = int(r.start // step) + 1
        while k * step < r.end - _EPS:
            cuts.append(k * step)
            k += 1
        cuts.append(r.end)
        pieces: list[classify.ClassificationRegion] = []
        for a, b in itertools.pairwise(cuts):
            values = db[int(a // step) : max(int(a // step) + 1, math.ceil(b / step - _EPS))]
            loud = bool(values) and sum(values) / len(values) > LOUD_DB
            bucket = OTHER_SOUND if loud else r.bucket
            if pieces and pieces[-1].bucket == bucket:
                pieces[-1] = replace(pieces[-1], end=b)
            else:
                pieces.append(replace(r, start=a, end=b, bucket=bucket))
        out.extend(pieces)
    return out


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
        self._language = transcript.get("language") if transcript else None
        # audio classification
        self._regions = None if regions is None else _relabel_loud(regions, loudness)
        self._r_starts = [r.start for r in self._regions or []]
        self._r_ends = [r.end for r in self._regions or []]
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
        self._ocr_interval = float(ocr_document.get("interval", ocr.DEFAULT_INTERVAL)) if ocr_document else 0.0
        self._o_frames = sorted(f["time"] for f in ocr_document["frames"]) if ocr_document else []

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

    # --- the whole of each signal, for readers that summarise a recording (media_report.py) ------------

    @property
    def class_regions(self) -> list[classify.ClassificationRegion] | None:
        """The audio classification's regions in time order, or None without one."""
        return None if self._regions is None else list(self._regions)

    @property
    def silences(self) -> list[tuple[float, float]] | None:
        """The silences of at least `MIN_SILENCE` seconds as (start, end), or None without a scan."""
        return list(zip(self._s_starts, self._s_ends, strict=True)) if self._silence_on else None

    @property
    def silence_threshold_db(self) -> float | None:
        return self._silence_db

    @property
    def cuts(self) -> list[tuple[float, float]] | None:
        """The scene cuts as (time, score), or None without scene signals."""
        return None if self._scenes is None else [(s["time"], s["score"]) for s in self._scenes["scenes"]]

    @property
    def loudness_db(self) -> list[float] | None:
        """The 1 s loudness values (dBFS), or None without a stored curve."""
        return None if self._loudness is None else list(self._loudness.db)

    @property
    def loudness_window_seconds(self) -> float | None:
        return None if self._loudness is None else self._loudness.window_seconds

    @property
    def words(self) -> list[str] | None:
        """Every transcribed word in time order, or None without a transcript."""
        return None if self._words is None else [w["word"] for w in self._words]

    @property
    def language(self) -> str | None:
        return self._language

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
            region = {"start": _r(a), "end": _r(b), "class": r.bucket, "score": _r(r.score)}
            if r.bucket == OTHER_SOUND:
                region["raw_class"] = classify.SILENCE_BUCKET
            regions.append(region)
        ranked = sorted(seconds, key=lambda c: (-round(seconds[c], 6), _CLASS_ORDER.get(c, 99)))
        length = max(end - start, _EPS)
        return {
            "class": ranked[0] if ranked else None,
            "raw_class": classify.SILENCE_BUCKET if ranked and ranked[0] == OTHER_SOUND else None,
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
        inside_exact = any(a <= time < b for a, b in found)
        inside = inside_exact and not self._speaking_at(time)
        intervals = [{"start": _r(max(a, start)), "end": _r(min(b, end))} for a, b in found]
        return {
            "threshold_db": self._silence_db,
            "inside": inside,
            "inside_exact": inside_exact,
            "intervals": intervals[:MAX_SILENCES],
            "truncated": len(intervals) > MAX_SILENCES,
        }

    def _speaking_at(self, time: float) -> bool:
        """Whether a transcribed word is being spoken at `time`, counting `SPEECH_ONSET_SLACK` before its start (False
        without a transcript)."""
        if not self._words:
            return False
        lo = bisect_right(self._w_ends, time)  # words ending at or before `time` are over
        hi = bisect_right(self._w_starts, time + SPEECH_ONSET_SLACK)
        return any(w["start"] - SPEECH_ONSET_SLACK <= time < w["end"] for w in self._words[lo:hi])

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
            else {
                "time": _r(cuts[near]),
                "score": self._scenes["scenes"][near]["score"],
                "kind": self._scenes["scenes"][near].get("kind", scenes.KIND_CUT),
            },
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
        age, stale, note = self._staleness(state, time)
        cut = len(text) > MAX_TEXT_CHARS or len(lines) > MAX_TEXT_LINES
        return {
            "time": _r(state["time"]),
            "until": _r(state["until"]),
            "age_seconds": age,
            "stale": stale,
            "note": note,
            "text": text[:MAX_TEXT_CHARS],
            "lines": lines[:MAX_TEXT_LINES],
            "truncated": cut,
            "more": max(0, len(overlapping) - (1 if state in overlapping else 0)),
        }

    def _staleness(self, state: dict[str, Any], time: float) -> tuple[float, bool, str | None]:
        """`(age_seconds, stale, note)` for a text state seen at `time`: the age is from the last OCR frame that saw
        this text (the frame that read it, or a later one that found it unchanged) up to `time`."""
        seen = state["time"]
        for t in self._o_frames[
            bisect_left(self._o_frames, state["time"]) : bisect_left(self._o_frames, state["until"])
        ]:
            if t > time:
                break
            seen = t
        age = _r(max(0.0, time - seen))
        reasons = []
        if self._scenes is not None:
            after = self._cuts[bisect_right(self._cuts, seen) : bisect_right(self._cuts, time)]
            if after:
                reasons.append(f"the picture changed at {after[0]:g} s, after this text was read")
        limit = STALE_INTERVALS * self._ocr_interval
        if age > limit:
            reasons.append(f"it was read {age:g} s before this time (more than {limit:g} s)")
        if not reasons:
            return age, False, None
        return age, True, "this text may no longer be on screen: " + "; ".join(reasons)


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
