"""Suggest highlight candidates from the stored signals (SPL-48): a deterministic first pass for the agent.

`suggest_highlights(media, asset, preset="default", count=10, ...)` slides a window over the whole recording, scores
each window from the signals the other tools stored (speech density, loudness, applause / music / loud non-speech
sound, scene and screen changes, screen activity, on-screen text changes and keyword hits, minus silence), picks the
best non-overlapping ones, snaps their edges to natural boundaries and returns them ranked, each with the reasons it
was chosen. It never decodes media and never runs a model: it reads the documents through `TimeIndex` (time_index.py),
the same way `what_is_happening` and `media_report` do. It proposes, the agent (or person) judges: nothing here
decides what is a good highlight, and the weights are the knob for what "good" means for a gig or a coding stream.

Method (every step is deterministic: the same stored signals give the same answer)
-------------------------------------------------------------------------------
1. A 1 s grid. Each signal becomes one value per second (words started in the second, dB, class seconds, activity,
   silent seconds), with running sums, so any window's value is two lookups (a sparse table gives the window maximum).
2. Windows of `target_seconds` (default 30 clamped to `[min_seconds, max_seconds]`) every `HOP_SECONDS` = 5 s over
   `[start, end]` (default the whole asset); the normalisation below is always fitted on the whole recording.
3. Per window, per signal, a feature in 0..1, normalised against the recording's OWN distribution so that quiet and
   loud recordings, chatty and silent ones, all work: `(value - median) / (high - median)` clipped to 0..1, where the
   median is over all windows and `high` is the 98th percentile (at least `median + MIN_RANGE[signal]`, so that a
   recording with no real peaks does not turn noise into full marks). Steady background therefore scores 0 and only
   what stands out scores. The class shares and keyword coverage are fractions of the window, so they use 1.0 as the
   high end. The signals (the keys of a weights dict):

   speech     words per second from the transcript (steady chatter is the median, a burst of speech scores)
   loudness   half the window's peak dB, half its mean dB (stored 1 s loudness curve)
   applause   share of the window the audio class is `applause_crowd` or `laughter`
   music      share that is `music` or `singing`
   noise      share that is `other_sound`: loud sound YAMNet filed under silence_other (time_index.py)
   scene      scene cuts and text-only screen changes per window (a rate per `target_seconds`)
   activity   screen activity (0.7 mean, 0.3 max of the 0..1 per second curve)
   ocr        times the on-screen text changed in the window (a rate, like scene)
   keywords   `keywords` found in the on-screen text (coverage of the window) or spoken (up to half the value);
              case-insensitive; omitted when no keywords are in force
   silence    PENALTY, not a feature: how much more of the window is silent than in the median window (the stored
              silence scan; the quiet `silence_other` regions when there is no scan; a recording that is mostly
              silence between its peaks is not punished for it), times this weight, is subtracted from the score

4. A window's score is `sum(weight_i * feature_i) / sum(weight_i)` over the signals that exist and have a weight
   above 0 (so a missing signal is omitted and the rest renormalised: the result's `signals.used` says which were
   used), minus `silence_weight * excess_silence`, clipped to 0..1. `score_parts` is each signal's term of that sum
   (the penalty is negative), so the parts add up to the score before snapping.
5. Greedy non-maximum suppression: the best window first, then the next best that is at least `min_gap_seconds` away
   from every pick (windows of equal score: the earlier first), until `count` or no window scores `MIN_SCORE` = 0.05 or more.
6. Growing and snapping (`snap=True`): a peak longer than the window is taken whole (the window grows by 5 s on
   whichever side has a next 5 s that alone scores at least half of the window's score, up to `max_seconds`). Then the start moves to the nearest boundary at or before it, within `SNAP_TOLERANCE` = 10 s
   (a start inside a silence moves to that silence's end); the end moves to the nearest boundary at or after it (an
   end inside a silence moves to that silence's start). A boundary is, in this order of preference, a silence
   gap's end / start (a stored silence of 0.5 s or more), a sentence boundary (a gap above 0.4 s between transcribed
   words), a scene change. If the result is longer than `max_seconds` the side that grew most is put back, if shorter
   than `min_seconds` both are; a pick never crosses `min_gap_seconds` of another pick. The candidate is then scored
   again on its final edges (same normalisation) and the candidates are ranked by that final score; `snapped` says
   what moved. The edges are plain seconds on the asset's timeline, usable as they are by an edit plan.

Presets (weights; the silence column is the penalty strength)
-------------------------------------------------------------
    preset    speech loudness applause music noise scene activity ocr keywords silence  default keywords
    default     1.0     1.0      1.0    0.5   0.3   0.7    0.5    0.5    1.5     0.4    -
    gig         0.2     1.5      2.0    1.5   0.3   0.2    0.1    0.0    1.0     0.5    -
    stream      1.5     0.5      0.3    0.1   0.2   1.0    0.8    1.0    2.0     0.4    FAILED error traceback exception
    lecture     1.5     0.2      0.3    0.0   0.0   1.5    0.3    1.2    1.5     0.3    -

`weights={"signal": value}` overrides single entries of the preset (unknown signal or a negative / non-finite value is
a `HighlightsError` that lists the valid signals; 0 switches a signal off). `keywords` replaces the preset's default
keywords (`[]` means none).

The result (version 1, stable)
------------------------------
Keys are never removed or renamed within a version; times are seconds on the asset's timeline, rounded to 3 decimals.

    {"version": 1, "asset": "a_...", "preset": "gig",
     "params": {"count", "min_seconds", "max_seconds", "target_seconds", "start", "end", "min_gap_seconds", "snap",
                "hop_seconds", "snap_tolerance_seconds", "keywords": [...],
                "weights": {every signal incl. silence: the effective weight}},
     "signals": {"available": {audio, loudness, silence, speech, scenes, ocr},   # as time_index
                 "missing": [{"signal", "how"}],
                 "used": [the weight signals that scored]},                      # speech, loudness, ...
     "candidates": [{"rank": 1, "start": 1200.0, "end": 1230.0, "duration": 30.0, "score": 0.83,
                     "reasons": [{"signal", "weight" (normalised, 0..1), "value" (the 0..1 feature), "note"}],
                     "score_parts": {"applause": 0.3, ...},
                     "snapped": {"start": "silence"|"sentence"|"scene"|null, "end": ..., "from": {"start", "end"}},
                     "title_hint"?, "transcript_excerpt"?, "ocr_excerpt"?}],
     "notes": ["..."]}

`reasons` are in order of contribution and say what was found, e.g. "applause 8s at 205-213", "speech 3.1 words/s",
"loudness peak -9 dB, mean -14 dB", "scene cut at 130", "on-screen text with 'FAILED': FAILED tests/...", "12% of the
window is silent". `title_hint` is the first line of the on-screen text (the one with a keyword if there is one) or
the first words spoken; `transcript_excerpt` and `ocr_excerpt` are at most 240 characters.

Errors: `AssetNotFoundError` for an unknown asset; `HighlightsError` for bad arguments; `NoSignalsError` (a
`HighlightsError`) when nothing that can score is stored, with the commands that make signals (`report --prepare`,
`transcribe`, `scenes`, `ocr`).

Cost: about 0.1 s of work for 3 hours on top of loading the stored documents (see docs/highlights.md); no per-window
Python loop is over the whole recording.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from typing import Any

import numpy as np

from split_video.editor import classify
from split_video.media_store import MediaStore
from split_video.time_index import OTHER_SOUND, TimeIndex
from split_video.time_index import SIGNALS as INDEX_SIGNALS

VERSION = 1
HOP_SECONDS = 5.0
SNAP_TOLERANCE = 10.0
SENTENCE_GAP = 0.4
DEFAULT_TARGET = 30.0
DEFAULT_COUNT = 10
MAX_COUNT = 100
DEFAULT_MIN_SECONDS = 15.0
DEFAULT_MAX_SECONDS = 60.0
DEFAULT_MIN_GAP = 10.0
EXCERPT_CHARS = 240
GROW_KEEP = 0.5
MIN_SCORE = 0.05
_EPS = 1e-9

POSITIVE = ("speech", "loudness", "applause", "music", "noise", "scene", "activity", "ocr", "keywords")
PENALTY = "silence"
WEIGHT_SIGNALS = (*POSITIVE, PENALTY)

# One row per preset, in the order of `WEIGHT_SIGNALS` (speech ... keywords, then the silence penalty).
_ROWS = {
    "default": (1.0, 1.0, 1.0, 0.5, 0.3, 0.7, 0.5, 0.5, 1.5, 0.4),
    "gig": (0.2, 1.5, 2.0, 1.5, 0.3, 0.2, 0.1, 0.0, 1.0, 0.5),
    "stream": (1.5, 0.5, 0.3, 0.1, 0.2, 1.0, 0.8, 1.0, 2.0, 0.4),
    "lecture": (1.5, 0.2, 0.3, 0.0, 0.0, 1.5, 0.3, 1.2, 1.5, 0.3),
}
_KEYWORDS = {"stream": ["FAILED", "error", "traceback", "exception"]}
PRESETS: dict[str, dict[str, Any]] = {
    name: {"weights": dict(zip(WEIGHT_SIGNALS, row, strict=True)), "keywords": list(_KEYWORDS.get(name, []))}
    for name, row in _ROWS.items()
}

# Smallest spread (high minus median, in the signal's own unit) that counts as a real peak; see the docstring.
MIN_RANGE = {
    "speech": 1.0,  # words per second
    "loudness_peak": 6.0,  # dB
    "loudness_mean": 6.0,
    "scene": 2.0,  # changes per `target_seconds`
    "activity_mean": 0.3,
    "activity_max": 0.3,
    "ocr": 2.0,
}
# Which stored signals (the `TimeIndex.available` keys) a weight signal needs; keywords can come from either.
NEEDS: dict[str, tuple[str, ...]] = {
    "speech": ("speech",),
    "loudness": ("loudness",),
    "applause": ("audio",),
    "music": ("audio",),
    "noise": ("audio",),
    "scene": ("scenes",),
    "activity": ("scenes",),
    "ocr": ("ocr",),
    "keywords": ("speech", "ocr"),
}
_APPLAUSE = ("applause_crowd", "laughter")
_MUSIC = ("music", "singing")
_LABEL = {"applause": "applause", "music": "music", "noise": "loud non-speech sound"}


class HighlightsError(ValueError):
    """A bad argument (unknown preset or signal, negative weight, bad span or count). The message says what is allowed."""


class NoSignalsError(HighlightsError):
    """Nothing that can score a window is stored for the asset. The message names the commands that make signals."""


# --- arguments ----------------------------------------------------------------------------------------


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise HighlightsError(f"{name} must be a number")
    return float(value)


def parse_weights(text: str | dict[str, Any] | None) -> dict[str, float] | None:
    """Weights from a JSON object (`{"speech": 2}`) or a compact `speech:2,applause:0.5` / `speech=2` string (what the
    HTTP query and the CLI's repeated `--weight` give). A dict passes through. `HighlightsError` for a malformed one."""
    if text is None or isinstance(text, dict):
        return text
    body = text.strip()
    if not body:
        return None
    if body.startswith("{"):
        try:
            found = json.loads(body)
        except ValueError:
            raise HighlightsError(f"weights is not valid JSON: {body[:60]!r}") from None
        if not isinstance(found, dict):
            raise HighlightsError('weights must be a JSON object like {"speech": 2}')
        return found
    out: dict[str, float] = {}
    for part in body.split(","):
        name, sep, value = part.replace("=", ":").partition(":")
        if not sep:
            raise HighlightsError(f"weights: expected signal:value, got {part.strip()!r}")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            raise HighlightsError(f"weights: {value.strip()!r} is not a number (for {name.strip()})") from None
    return out


def parse_keywords(text: str | list[str] | None) -> list[str] | None:
    """Keywords from a comma separated string or a list; None stays None (the preset's default)."""
    if text is None:
        return None
    parts = text.split(",") if isinstance(text, str) else list(text)
    out: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            raise HighlightsError("keywords must be strings")
        word = part.strip()
        if word and word.lower() not in (k.lower() for k in out):
            out.append(word)
    return out


def _effective_weights(preset: str, override: dict[str, Any] | None) -> dict[str, float]:
    weights = dict(PRESETS[preset]["weights"])
    for name, value in (override or {}).items():
        if name not in WEIGHT_SIGNALS:
            raise HighlightsError(f"unknown weight signal {name!r}: valid signals are {', '.join(WEIGHT_SIGNALS)}")
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise HighlightsError(f"weight for {name} must be a number (got {value!r})")
        if value < 0:
            raise HighlightsError(f"weight for {name} must not be negative (got {value:g})")
        weights[name] = float(value)
    return weights


# --- the 1 s grid ---------------------------------------------------------------------------------------


def _paint(arr: np.ndarray, a: float, b: float) -> None:
    """Add the overlap of `[a, b)` with each 1 s cell to `arr` (partial cells count by their overlap)."""
    n = len(arr)
    a, b = max(0.0, a), min(float(n), b)
    if b <= a:
        return
    i, j = int(a), min(math.ceil(b), n)
    if j - i <= 1:
        arr[i] += b - a
        return
    arr[i] += i + 1 - a
    arr[j - 1] += b - (j - 1)
    arr[i + 1 : j - 1] += 1.0


def _per_second(values: list[float], step: float, n: int, fill: float) -> np.ndarray:
    """A curve sampled every `step` seconds as one value per second, padded with `fill` to `n` values."""
    arr = np.asarray(values, dtype=float)
    if step != 1.0 and len(arr):
        arr = arr[np.minimum((np.arange(n) / step).astype(int), len(arr) - 1)]
    out = np.full(n, fill, dtype=float)
    out[: min(n, len(arr))] = arr[:n]
    return out


class _RangeMax:
    """Maximum of `arr[i:j]` for arrays of i, j at once (a sparse table)."""

    def __init__(self, arr: np.ndarray) -> None:
        self.table = [arr]
        k = 1
        while 2 * k <= len(arr):
            prev = self.table[-1]
            self.table.append(np.maximum(prev[:-k], prev[k:]))
            k *= 2

    def query(self, i: np.ndarray, j: np.ndarray) -> np.ndarray:
        length = np.maximum(j - i, 1)
        level = np.floor(np.log2(length)).astype(int)
        out = np.empty(len(i))
        for lv in np.unique(level):
            pick = level == lv
            t = self.table[lv]
            ii, jj = i[pick], j[pick]
            out[pick] = np.maximum(t[ii], t[jj - (1 << int(lv))])
        return out


def _cum(arr: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(arr)])


class _Grid:
    """Every stored signal as per-second arrays and running sums, ready for vectorised windows."""

    def __init__(self, index: TimeIndex, keywords: list[str]) -> None:
        self.index = index
        self.duration = index.duration
        n = self.n = max(1, math.ceil(index.duration))
        self.cuts = np.asarray([c[0] for c in index.cut_kinds or []], dtype=float)
        self.cut_kinds = index.cut_kinds
        self.cum: dict[str, np.ndarray] = {}
        self.maxes: dict[str, _RangeMax] = {}
        regions = index.class_regions
        if regions is not None:
            groups = {"applause": _APPLAUSE, "music": _MUSIC, "noise": (OTHER_SOUND,)}
            for name, classes in groups.items():
                arr = np.zeros(n)
                for r in regions:
                    if r.bucket in classes:
                        _paint(arr, r.start, r.end)
                self.cum[name] = _cum(np.minimum(arr, 1.0))
        if index.loudness_db is not None:
            db = np.nan_to_num(
                _per_second(index.loudness_db, index.loudness_window_seconds or 1.0, n, -120.0), neginf=-120.0
            )
            self.cum["loudness"] = _cum(db)
            self.maxes["loudness"] = _RangeMax(db)
        words = index.timed_words
        self.word_starts = np.asarray([w[0] for w in words or []], dtype=float)
        if words is not None:
            first = np.minimum(np.floor(self.word_starts).astype(int), n - 1)
            self.cum["speech"] = _cum(np.bincount(first, minlength=n).astype(float))
        activity = index.activity
        if activity is not None:
            act = _per_second(activity[1], activity[0], n, 0.0)
            self.cum["activity"] = _cum(act)
            self.maxes["activity"] = _RangeMax(act)
        self.states = index.text_states
        self.state_times = np.asarray([s["time"] for s in self.states or [] if s["time"] > _EPS], dtype=float)
        # silences: the stored scan, else the quiet silence_other regions of the classification
        self.gaps = self._gaps(index)
        sil = np.zeros(n)
        for a, b in self.gaps or []:
            _paint(sil, a, b)
        self.cum["silence"] = _cum(np.minimum(sil, 1.0))
        # sentence boundaries from the gaps between words
        self.sent_starts: list[float] = []
        self.sent_ends: list[float] = []
        if words:
            self.sent_starts.append(words[0][0])
            running_end = words[0][1]
            for w in words[1:]:
                if w[0] - running_end > SENTENCE_GAP:
                    self.sent_ends.append(running_end)
                    self.sent_starts.append(w[0])
                running_end = max(running_end, w[1])
            self.sent_ends.append(running_end)
        # keywords
        self.keywords = keywords
        self.kw_ocr: list[tuple[float, float, str, str]] = []  # (time, until, keyword, line) of matching text states
        self.kw_spoken: list[tuple[float, str]] = []
        if keywords:
            lowered = [k.lower() for k in keywords]
            ocr_on = np.zeros(n)
            for s in self.states or []:
                text = s["text"].lower()
                hit = next((k for k, low in zip(keywords, lowered, strict=True) if low in text), None)
                if hit is not None:
                    line = next((ln for ln in s["lines"] if hit.lower() in ln.lower()), s["lines"][0])
                    self.kw_ocr.append((s["time"], s["until"], hit, line))
                    _paint(ocr_on, s["time"], s["until"])
            spoken = np.zeros(n)
            for start, _, word in words or []:
                low = word.lower()
                hit = next((k for k, kl in zip(keywords, lowered, strict=True) if kl in low), None)
                if hit is not None:
                    self.kw_spoken.append((start, hit))
                    spoken[min(int(start), n - 1)] += 1.0
            if self.states is not None:
                self.cum["kw_ocr"] = _cum(np.minimum(ocr_on, 1.0))
            if words is not None:
                self.cum["kw_spoken"] = _cum(spoken)

    @staticmethod
    def _gaps(index: TimeIndex) -> list[tuple[float, float]] | None:
        if index.silences is not None:
            return index.silences
        regions = index.class_regions
        if regions is None:
            return None
        return [(r.start, r.end) for r in regions if r.bucket == classify.SILENCE_BUCKET and r.end - r.start >= 1.0]

    def has(self, signal: str) -> bool:
        available = self.index.available
        return (
            any(available[k] for k in NEEDS[signal])
            if signal != "keywords"
            else bool(self.keywords and ("kw_ocr" in self.cum or "kw_spoken" in self.cum))
        )

    # --- raw values for windows [s, e) (float arrays) ----------------------------------------------------

    def raw(self, s: np.ndarray, e: np.ndarray, target: float) -> dict[str, np.ndarray]:
        si = np.clip(np.floor(s + _EPS).astype(int), 0, self.n - 1)
        ei = np.clip(np.ceil(e - _EPS).astype(int), si + 1, self.n)
        length = (ei - si).astype(float)
        out: dict[str, np.ndarray] = {}

        def mean(name: str) -> np.ndarray:
            c = self.cum[name]
            return (c[ei] - c[si]) / length

        for name in ("applause", "music", "noise"):
            if name in self.cum:
                out[name] = mean(name)
        if "speech" in self.cum:
            out["speech"] = mean("speech")
        if "loudness" in self.cum:
            out["loudness_mean"] = mean("loudness")
            out["loudness_peak"] = self.maxes["loudness"].query(si, ei)
        if "activity" in self.cum:
            out["activity_mean"] = mean("activity")
            out["activity_max"] = self.maxes["activity"].query(si, ei)
        if self.cut_kinds is not None:
            count = np.searchsorted(self.cuts, e - _EPS, side="left") - np.searchsorted(
                self.cuts, s - _EPS, side="left"
            )
            out["scene"] = count * target / np.maximum(e - s, 1.0)
        if self.states is not None:
            count = np.searchsorted(self.state_times, e - _EPS) - np.searchsorted(self.state_times, s - _EPS)
            out["ocr"] = count * target / np.maximum(e - s, 1.0)
        if self.keywords:
            ocr_share = mean("kw_ocr") if "kw_ocr" in self.cum else np.zeros(len(s))
            hits = mean("kw_spoken") * length if "kw_spoken" in self.cum else np.zeros(len(s))
            out["keywords"] = np.minimum(1.0, np.minimum(1.0, ocr_share / 0.5) + 0.5 * np.minimum(1.0, hits / 2.0))
        out["silence"] = mean("silence")
        return out


def _fit(values: np.ndarray, min_range: float | None) -> tuple[float, float]:
    """`(low, high)` of the normalisation: the median, and the 98th percentile or `low + min_range`, or 1.0 for a share."""
    low = float(np.percentile(values, 50))
    if min_range is None:
        return low, 1.0
    return low, max(float(np.percentile(values, 98)), low + min_range)


def _unit(values: np.ndarray, fit: tuple[float, float]) -> np.ndarray:
    low, high = fit
    if high - low <= _EPS:
        return np.zeros_like(values)
    return np.clip((values - low) / (high - low), 0.0, 1.0)


class _Scorer:
    """Turns raw window values into the per-signal features, the score and its parts (the normalisation is fitted once,
    on the regular windows of the whole recording, and reused for the re-scoring of snapped candidates)."""

    def __init__(self, grid: _Grid, weights: dict[str, float], target: float, s: np.ndarray, e: np.ndarray) -> None:
        self.grid, self.target = grid, target
        raw = grid.raw(s, e, target)
        self.fits: dict[str, tuple[float, float]] = {}
        for key, values in raw.items():
            self.fits[key] = _fit(values, MIN_RANGE.get(key))
        self.used = [sig for sig in POSITIVE if weights.get(sig, 0) > 0 and grid.has(sig)]
        total = sum(weights[sig] for sig in self.used)
        self.weights = {sig: (weights[sig] / total if total else 0.0) for sig in self.used}
        self.penalty = weights[PENALTY]

    def features(self, raw: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        u = {k: _unit(v, self.fits[k]) for k, v in raw.items() if k in self.fits}
        feats: dict[str, np.ndarray] = {}
        for sig in self.used:
            if sig == "loudness":
                feats[sig] = 0.5 * u["loudness_peak"] + 0.5 * u["loudness_mean"]
            elif sig == "activity":
                feats[sig] = 0.7 * u["activity_mean"] + 0.3 * u["activity_max"]
            else:
                feats[sig] = u[sig]
        feats[PENALTY] = u["silence"]
        return feats

    def score(self, s: np.ndarray, e: np.ndarray) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, np.ndarray]]:
        raw = self.grid.raw(s, e, self.target)
        feats = self.features(raw)
        parts = {sig: self.weights[sig] * feats[sig] for sig in self.used}
        parts[PENALTY] = -self.penalty * feats[PENALTY]
        total = sum(parts.values())
        return np.clip(total, 0.0, 1.0), feats, parts


# --- snapping --------------------------------------------------------------------------------------------


class _Snapper:
    def __init__(self, grid: _Grid) -> None:
        gaps = sorted(grid.gaps or [])
        self.gap_starts = [a for a, _ in gaps]
        self.gap_ends = [b for _, b in gaps]
        self.sent_starts, self.sent_ends = grid.sent_starts, grid.sent_ends
        self.cuts = sorted(float(c) for c in grid.cuts)

    def start(self, t: float, low: float) -> tuple[float, str | None]:
        """The nearest boundary at or before `t` (a silence end, else a sentence start, else a cut) within the
        tolerance and not before `low`; inside a silence, its end."""
        i = bisect_right(self.gap_starts, t + _EPS) - 1
        if i >= 0 and self.gap_ends[i] > t and self.gap_ends[i] - t <= SNAP_TOLERANCE:
            return self.gap_ends[i], "silence"
        for kind, values in (("silence", self.gap_ends), ("sentence", self.sent_starts), ("scene", self.cuts)):
            j = bisect_right(values, t + _EPS) - 1
            if j >= 0 and t - SNAP_TOLERANCE <= values[j] <= t + _EPS and values[j] >= low:
                return values[j], kind
        return t, None

    def end(self, t: float, high: float) -> tuple[float, str | None]:
        """The nearest boundary at or after `t` (a silence start, else a sentence end, else a cut) within the tolerance
        and not after `high`; inside a silence, its start."""
        i = bisect_left(self.gap_ends, t - _EPS)
        if i < len(self.gap_ends) and self.gap_starts[i] < t and t - self.gap_starts[i] <= SNAP_TOLERANCE:
            return self.gap_starts[i], "silence"
        for kind, values in (("silence", self.gap_starts), ("sentence", self.sent_ends), ("scene", self.cuts)):
            j = bisect_left(values, t - _EPS)
            if j < len(values) and t - _EPS <= values[j] <= t + SNAP_TOLERANCE and values[j] <= high:
                return values[j], kind
        return t, None

    def snap(
        self, s: float, e: float, low: float, high: float, min_seconds: float, max_seconds: float
    ) -> tuple[float, float, str | None, str | None]:
        ns, sk = self.start(s, low)
        ne, ek = self.end(e, high)
        ns, ne = max(ns, low), min(ne, high)
        if ne - ns > max_seconds + _EPS:  # put back the side that grew most, then the other if need be
            for side in sorted(("s", "e"), key=lambda k: -(s - ns if k == "s" else ne - e)):
                if side == "s":
                    ns, sk = s, None
                else:
                    ne, ek = e, None
                if ne - ns <= max_seconds + _EPS:
                    break
        if ne - ns < min_seconds - _EPS:
            ns, ne, sk, ek = s, e, None, None
        return ns, ne, sk, ek


def _grow(scorer: _Scorer, s: float, e: float, low: float, high: float, max_seconds: float) -> tuple[float, float]:
    """Widen the window by `HOP_SECONDS` on whichever side has the better next chunk, as long as that chunk alone
    scores at least `GROW_KEEP` of the window's own score, within `max_seconds` and `[low, high]`: a peak longer than
    the target window is taken whole."""
    ref = float(scorer.score(np.asarray([s]), np.asarray([e]))[0][0])
    while e - s + HOP_SECONDS <= max_seconds + _EPS and ref > 0:
        tries = []
        if s - HOP_SECONDS >= low - _EPS:
            tries.append((s - HOP_SECONDS, s))
        if e + HOP_SECONDS <= high + _EPS:
            tries.append((e, e + HOP_SECONDS))
        if not tries:
            break
        scores = scorer.score(np.asarray([t[0] for t in tries]), np.asarray([t[1] for t in tries]))[0]
        best = int(np.argmax(scores))  # a tie goes to the start side (the first)
        if scores[best] < ref * GROW_KEEP:
            break
        s, e = (tries[best][0], e) if tries[best][1] == s else (s, tries[best][1])
    return s, e


# --- describing a candidate --------------------------------------------------------------------------------


def _runs(regions: list[Any], classes: tuple[str, ...], a: float, b: float) -> tuple[float, float, float]:
    """`(seconds, run_start, run_end)`: the seconds of `classes` inside `[a, b)` and the span of the longest run (pieces
    at most 2 s apart count as one)."""
    pieces = sorted(
        (max(r.start, a), min(r.end, b)) for r in regions if r.bucket in classes and r.end > a and r.start < b
    )
    total = sum(y - x for x, y in pieces)
    best = (0.0, 0.0, 0.0)
    cur: list[float] | None = None
    for x, y in pieces:
        if cur is not None and x - cur[1] <= 2.0:
            cur[1] = max(cur[1], y)
            cur[2] += y - x
        else:
            cur = [x, y, y - x]
        if cur[1] - cur[0] > best[2] - best[1] or best == (0.0, 0.0, 0.0):
            best = (cur[2], cur[0], cur[1])
    return total, best[1], best[2]


def _clip(text: str) -> str:
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1].rstrip() + "…"


def _r(v: float) -> float:
    return round(float(v), 3)


class _Describer:
    def __init__(self, grid: _Grid) -> None:
        self.grid = grid
        words = grid.index.timed_words
        self.words = words
        self.w_starts = [w[0] for w in words or []]
        self.regions = grid.index.class_regions or []

    def reasons(
        self, a: float, b: float, feats: dict[str, float], parts: dict[str, float], weights: dict[str, float], raw: dict
    ) -> list[dict[str, Any]]:
        out = []
        for sig, part in parts.items():
            if sig == PENALTY:
                if part < -0.005:
                    out.append(
                        {
                            "signal": sig,
                            "weight": _r(weights.get(sig, 0.0)),
                            "value": _r(feats[PENALTY]),
                            "note": f"{raw['silence'] * 100:.0f}% of the window is silent",
                            "_part": part,
                        }
                    )
                continue
            if part < 0.01:
                continue
            out.append(
                {
                    "signal": sig,
                    "weight": _r(weights[sig]),
                    "value": _r(feats[sig]),
                    "note": self._note(sig, a, b, raw),
                    "_part": part,
                }
            )
        out.sort(key=lambda r: -abs(r["_part"]))
        for r in out:
            del r["_part"]
        return out

    def _note(self, sig: str, a: float, b: float, raw: dict) -> str:
        if sig in ("applause", "music", "noise"):
            classes = {"applause": _APPLAUSE, "music": _MUSIC, "noise": (OTHER_SOUND,)}[sig]
            seconds, x, y = _runs(self.regions, classes, a, b)
            label = _LABEL[sig]
            if (
                sig == "applause"
                and seconds
                and all(
                    r.bucket == "laughter" for r in self.regions if r.bucket in classes and r.end > a and r.start < b
                )
            ):
                label = "laughter"
            if sig == "music":
                singing = _runs(self.regions, ("singing",), a, b)[0]
                if singing > seconds - singing:
                    label = "singing"
            return f"{label} {seconds:.0f}s at {x:g}-{y:g}"
        if sig == "speech":
            return f"speech {raw['speech']:.1f} words/s"
        if sig == "loudness":
            return f"loudness peak {raw['loudness_peak']:.0f} dB, mean {raw['loudness_mean']:.0f} dB"
        if sig == "scene":
            return self._scene_note(a, b)
        if sig == "activity":
            return f"screen activity mean {raw['activity_mean']:.2f}, peak {raw['activity_max']:.2f}"
        if sig == "ocr":
            n = bisect_left(self.grid.state_times, b - _EPS) - bisect_left(self.grid.state_times, a - _EPS)
            return f"on-screen text changes {n}x"
        return self._keyword_note(a, b)

    def _scene_note(self, a: float, b: float) -> str:
        found = [(t, k) for t, _, k in self.grid.cut_kinds or [] if a - _EPS <= t < b - _EPS]
        name = {"cut": "scene cut", "screen": "screen change"}
        if len(found) == 1:
            return f"{name.get(found[0][1], 'scene change')} at {found[0][0]:g}"
        first = ", ".join(f"{t:g}" for t, _ in found[:4])
        return f"{len(found)} scene/screen changes at {first}" + (", ..." if len(found) > 4 else "")

    def _keyword_note(self, a: float, b: float) -> str:
        seen = [(t, u, k, ln) for t, u, k, ln in self.grid.kw_ocr if u > a and t < b]
        if seen:
            k, ln = seen[0][2:]
            return f"on-screen text with {k!r}: {ln[:80]}"
        spoken = [(t, k) for t, k in self.grid.kw_spoken if a <= t < b]
        if spoken:
            return f"keyword {spoken[0][1]!r} spoken at {spoken[0][0]:g}"
        return "keyword"

    def texts(self, a: float, b: float) -> dict[str, str]:
        out: dict[str, str] = {}
        title = None
        states = self.grid.states or []
        mid = (a + b) / 2
        keyword = [(t, ln) for t, u, _, ln in self.grid.kw_ocr if u > a and t < b]
        up = [s for s in states if s["until"] > a and s["time"] < b]
        shown = next((s for s in up if s["time"] <= mid < s["until"]), up[-1] if up else None)
        if keyword:
            title = keyword[0][1]
        elif shown is not None and shown["lines"]:
            title = shown["lines"][0]
        if shown is not None and shown["lines"]:
            lines = shown["lines"]
            if keyword:
                kw_state = next((s for s in up if keyword[0][1] in s["lines"]), shown)
                lines = kw_state["lines"]
            out["ocr_excerpt"] = _clip(" / ".join(lines))
        if self.words:
            lo, hi = bisect_left(self.w_starts, a - _EPS), bisect_left(self.w_starts, b - _EPS)
            spoken = [w[2] for w in self.words[lo:hi]]
            if spoken:
                out["transcript_excerpt"] = _clip(" ".join(spoken))
                if title is None:
                    title = " ".join(spoken[:8])
        if title:
            out = {"title_hint": title[:80].strip(), **out}
        return out


# --- the engine ------------------------------------------------------------------------------------------


def suggest_from_index(
    index: TimeIndex,
    *,
    preset: str = "default",
    count: int = DEFAULT_COUNT,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    target_seconds: float | None = None,
    weights: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
    start: float | None = None,
    end: float | None = None,
    min_gap_seconds: float = DEFAULT_MIN_GAP,
    snap: bool = True,
) -> dict[str, Any]:
    """`suggest_highlights` on a loaded `TimeIndex` (see the module docstring)."""
    if preset not in PRESETS:
        raise HighlightsError(f"unknown preset {preset!r}: valid presets are {', '.join(PRESETS)}")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
        raise HighlightsError(f"count must be a whole number from 1 to {MAX_COUNT}")
    min_s, max_s = _number("min_seconds", min_seconds), _number("max_seconds", max_seconds)
    gap = _number("min_gap_seconds", min_gap_seconds)
    if min_s <= 0 or max_s < min_s:
        raise HighlightsError("min_seconds must be above 0 and max_seconds at least min_seconds")
    if gap < 0:
        raise HighlightsError("min_gap_seconds must not be negative")
    target = None if target_seconds is None else _number("target_seconds", target_seconds)
    if target is not None and not min_s <= target <= max_s:
        raise HighlightsError(f"target_seconds must be between min_seconds ({min_s:g}) and max_seconds ({max_s:g})")
    duration = index.duration
    lo_t = 0.0 if start is None else _number("start", start)
    hi_t = duration if end is None else _number("end", end)
    if lo_t < 0 or hi_t > duration + _EPS or hi_t <= lo_t:
        raise HighlightsError(f"start and end must satisfy 0 <= start < end <= {duration:g} (the asset's duration)")
    hi_t = min(hi_t, duration)
    effective = _effective_weights(preset, weights)
    kws = PRESETS[preset]["keywords"] if keywords is None else keywords
    notes: list[str] = []
    if keywords is None and kws:
        notes.append(f"keywords from the {preset} preset: {', '.join(kws)}")

    available = index.available
    base_signals = ("speech", "loudness", "applause", "music", "noise", "scene", "activity", "ocr")
    grid = _Grid(index, list(kws))
    scoring = [s for s in base_signals if grid.has(s)]
    if not scoring:
        raise NoSignalsError(
            "no stored signal can score a recording for highlights. Run `split-video report <target> --prepare` "
            "(loudness, silences, audio classes), `split-video transcribe <target>`, `split-video scenes <target>` "
            "and/or `split-video ocr <target>` first (MCP: media_report with prepare=true, transcribe_asset, "
            "analyze_scenes, ocr_asset; HTTP: POST /api/v1/assets/{id}/report/prepare, .../transcribe, .../scenes, "
            ".../ocr)"
        )
    span = hi_t - lo_t
    if span < min_s:
        notes.append(f"the span is {span:g} s, shorter than min_seconds ({min_s:g}): using the whole span")
        min_s = span
        max_s = max(max_s, span)
    length = min(span, min(max(DEFAULT_TARGET, min_s), max_s) if target is None else target)

    def windows(a: float, b: float) -> tuple[np.ndarray, np.ndarray]:
        first = np.arange(a, b - length + 1e-6, HOP_SECONDS)
        if len(first) == 0 or b - length - first[-1] > 1.0:
            first = np.append(first, b - length)
        return first, first + length

    starts, ends = windows(lo_t, hi_t)
    # the normalisation is fitted on the whole recording, so `start` / `end` only limit where candidates come from
    scorer = _Scorer(grid, effective, length, *windows(0.0, duration) if duration >= length else (starts, ends))
    if not scorer.used:
        raise HighlightsError(
            "every weight of the stored signals is 0: give at least one of "
            f"{', '.join(s for s in POSITIVE if grid.has(s))} a weight above 0"
        )
    unused = [s for s in POSITIVE if effective[s] > 0 and not grid.has(s) and s != "keywords"]
    if unused:
        notes.append(f"no stored data for {', '.join(unused)}: left out and the other weights renormalised")
    score, _, _ = scorer.score(starts, ends)
    order = np.lexsort((starts, -score))

    snapper = _Snapper(grid)
    describer = _Describer(grid)
    picked: list[tuple[float, float, str | None, str | None, float, float]] = []  # s, e, kinds, original s, e
    for idx in order:
        if len(picked) >= count or score[idx] < MIN_SCORE:
            break
        s, e = float(starts[idx]), float(ends[idx])
        if any(s < pe + gap and e > ps - gap for ps, pe, *_ in picked):
            continue
        low = max([lo_t] + [pe + gap for _, pe, *_ in picked if pe <= s + _EPS])
        high = min([hi_t] + [ps - gap for ps, *_ in picked if ps >= e - _EPS])
        ns, ne, sk, ek = (s, e, None, None)
        if snap:
            gs, ge = _grow(scorer, s, e, low, high, max_s)
            ns, ne, sk, ek = snapper.snap(gs, ge, low, high, min_s, max_s)
            if ne - ns < min_s - _EPS or ne - ns > max_s + _EPS:
                ns, ne, sk, ek = s, e, None, None
        picked.append((ns, ne, sk, ek, s, e))
    if not picked:
        notes.append(f"no window scored {MIN_SCORE:g} or more: nothing stands out with these weights")

    candidates: list[dict[str, Any]] = []
    if picked:
        ps = np.asarray([p[0] for p in picked])
        pe = np.asarray([p[1] for p in picked])
        final, feats, parts = scorer.score(ps, pe)
        raw = grid.raw(ps, pe, length)
        ranked = sorted(range(len(picked)), key=lambda k: (-final[k], ps[k]))
        for rank, k in enumerate(ranked, 1):
            a, b, sk, ek, os_, oe = picked[k]
            one = {key: float(v[k]) for key, v in feats.items()}
            part = {key: float(v[k]) for key, v in parts.items()}
            raw_k = {key: float(v[k]) for key, v in raw.items()}
            candidates.append(
                {
                    "rank": rank,
                    "start": _r(a),
                    "end": _r(b),
                    "duration": _r(b - a),
                    "score": _r(final[k]),
                    "reasons": describer.reasons(a, b, one, part, scorer.weights | {PENALTY: scorer.penalty}, raw_k),
                    "score_parts": {key: _r(v) for key, v in part.items() if abs(v) >= 0.0005},
                    "snapped": {"start": sk, "end": ek, "from": {"start": _r(os_), "end": _r(oe)}},
                    **describer.texts(a, b),
                }
            )
    if len(candidates) < count and picked:
        notes.append(f"{len(candidates)} candidates: no more non-overlapping windows scoring {MIN_SCORE:g} or more")
    return {
        "version": VERSION,
        "asset": index.asset.id,
        "preset": preset,
        "params": {
            "count": count,
            "min_seconds": _r(min_s),
            "max_seconds": _r(max_s),
            "target_seconds": _r(length),
            "start": _r(lo_t),
            "end": _r(hi_t),
            "min_gap_seconds": _r(gap),
            "snap": bool(snap),
            "hop_seconds": HOP_SECONDS,
            "snap_tolerance_seconds": SNAP_TOLERANCE,
            "keywords": list(kws),
            "weights": {k: effective[k] for k in WEIGHT_SIGNALS},
        },
        "signals": {
            "available": {k: available[k] for k in INDEX_SIGNALS},
            "missing": index.missing,
            "used": list(scorer.used),
        },
        "candidates": candidates,
        "notes": notes,
    }


def suggest_highlights(
    media: MediaStore,
    asset_id: str,
    *,
    preset: str = "default",
    count: int = DEFAULT_COUNT,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    target_seconds: float | None = None,
    weights: dict[str, Any] | str | None = None,
    keywords: list[str] | str | None = None,
    start: float | None = None,
    end: float | None = None,
    min_gap_seconds: float = DEFAULT_MIN_GAP,
    snap: bool = True,
) -> dict[str, Any]:
    """Ranked, non-overlapping highlight candidates for the asset (see the module docstring). Reads the stored signals
    only. `AssetNotFoundError` for an unknown asset, `HighlightsError` for bad arguments, `NoSignalsError` when nothing
    is stored."""
    return suggest_from_index(
        TimeIndex.load(media, asset_id),
        preset=preset,
        count=count,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        target_seconds=target_seconds,
        weights=parse_weights(weights),
        keywords=parse_keywords(keywords),
        start=start,
        end=end,
        min_gap_seconds=min_gap_seconds,
        snap=snap,
    )


def dumps(doc: dict[str, Any]) -> str:
    """The document as the CLI's `--json` prints it: compact JSON."""
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
