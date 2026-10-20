"""Media report: every stored signal of a recording in one bounded, time-bucketed JSON document (SPL-44).

`media_report(media, asset, bucket_seconds=None, include=None, prepare=False)` answers "what is in this recording?" in
one call, so that an agent does not need five tools: duration, silence gaps, the loudness curve, music / speech /
applause regions, scene changes, screen activity, a transcript summary and on-screen text, as compact JSON that is
bounded in size however long the recording is (default 40 KB, a 3 hour recording with every signal: about 24 KB).
It reuses `TimeIndex` (time_index.py) for every per-bucket value, so the numbers agree with `what_is_happening`.

    index-free use:  media_report.media_report(media, asset_id)               # reads what is stored, computes nothing
                     media_report.media_report(media, asset_id, prepare=True) # first makes the missing cheap signals

Arguments
---------
`bucket_seconds`: seconds per time bucket, `MIN_BUCKET_SECONDS` = 5 to `MAX_BUCKET_SECONDS` = 600. The default is
`max(10, ceil(duration / 180))`: 30 s for a 90 minute file, 60 s for 3 hours, so about 180 buckets. `buckets` asks for
a number of buckets instead (1 to 720; not together with `bucket_seconds`). A size that would give more than 720
buckets is an error. `include`: some of audio, loudness, silence, speech, scenes, ocr (default all); the rest are
`null` and listed in `signals.excluded`. `max_bytes`: the budget (`DEFAULT_MAX_BYTES` = 40000, at least 4096) for the
document as compact JSON (`encoded_size`). `AssetNotFoundError` for an unknown asset, `MediaReportError` for a bad
argument. A signal that was never computed is never an error: it is `null` and `signals.missing` says how to make it.

Preparing
---------
The report is read-only by default. `prepare=True` (CLI `--prepare`, MCP `prepare`, HTTP `POST
/assets/{id}/report/prepare` as an `analysis` job) first runs `prepare_signals`: the loudness curve (one streamed
decode, seconds for hours of audio), the silence scan at -35 dB (one ffmpeg pass) and the audio classification (YAMNet,
if its model is installed, else it is skipped with the reason). It NEVER transcribes, finds scenes or reads
on-screen text: those are heavy and explicit, and stay in `signals.missing` with the command that makes them. Each
signal is stored when it is whole, so a cancelled job leaves no partial signal.

The document (version 1, stable)
--------------------------------
Keys are never added or removed within a version; a part without data is `null`. Times are seconds, levels dBFS.

    {"version": 1,
     "asset": {"id", "path", "duration", "size", "has_audio", "has_video", "streams": ["video", "audio"],
               "width", "height", "fps"},
     "signals": {"available": {"audio", "loudness", "silence", "speech", "scenes", "ocr"},   # booleans
                 "missing": [{"signal", "how"}],      # absent signals the asset can have, with how to make them
                 "excluded": [...]},                   # left out by `include`
     "prepared": null | {"asset", "prepared": [...], "already": [...], "skipped": [{"signal", "reason"}]},
     "limits": {"max_bytes": 40000},
     "notices": {"coarsened": null | {"requested_bucket_seconds", "bucket_seconds", "reason": "max_bytes"},
                 "shrunk": null | {"transcript_chars", "screen_text_chars"},
                 "dropped": [...],                     # "transcript", "screen_text", "buckets", "everything"
                 "truncated": [...]},                  # lists that hold fewer items than their `count`
     "buckets": {"seconds": 60.0, "count": 180,        # bucket k covers [k * seconds, min(duration, (k + 1) * seconds))
                 "audio_class": [...], "audio_share": [...],        # the class with the most seconds, and its share
                 "loudness_mean_db": [...], "loudness_max_db": [...],
                 "silence_fraction": [...],                          # of the bucket, silences of 0.5 s or more
                 "words": [...], "words_per_minute": [...],
                 "scene_changes": [...], "activity_mean": [...], "activity_max": [...],
                 "screen_text_changed": [...],         # 1 when the text up at the end of the bucket differs from
                                                       # the bucket before's (0 otherwise)
                 "transcript": [...],                  # the first 40 characters of the words, shrinking to 20, 10
                 "screen_text": [...]},                # first line of the new text, 60 characters, only on a change
                # every column is one value per bucket, or null as a whole when its signal is missing or dropped
     "loudness": {"mean_db", "p95_db", "max_db", "curve_seconds", "curve": [60 whole dB values]},
     "regions": {"min_seconds": 10, "seconds_by_class": {...}, "count", "truncated", "items": [[start, end, class]]},
     "silence_gaps": {"min_seconds": 2, "threshold_db", "count", "truncated", "items": [[start, end]]},
     "scene_changes": {"count", "truncated", "items": [[time, score]]},   # the strongest 40, in time order
     "transcript": {"words", "language", "words_per_minute", "top_keywords": [[word, count]]}}  # 12, plain frequency

`regions` merges touching regions of one class and keeps the longest 60 of those of 10 s or more; `silence_gaps` keeps
the longest 40 of 2 s or more. A keyword is a word of 3 letters or more that is not an English stopword; this is a
count, not a ranking of moments. There is no per-bucket list of cut times: `scene_changes` has the strongest cuts.

Staying under `max_bytes`
-------------------------
The size depends on the number of buckets (at most 720), the snippet lengths and the list caps, not on the duration.
When the document is over `max_bytes` it degrades in this order, trying the budget after each step, and says so in
`notices` (it never exceeds the budget): shorter snippets (40/60, 20/30, 10/15 characters), no snippets, coarser
buckets (doubled up to 600 s, with the snippets again), shorter lists (halved down to none), no buckets, and last of
all a bare document `{"version", "asset": {"id"}, "signals", "notices": {"dropped": ["everything"]}, ...}`.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

from split_video import assets, project
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.jobs import CancelToken
from split_video.media_store import MediaStore
from split_video.time_index import SIGNALS, SILENCE_THRESHOLD_DB, TimeIndex

VERSION = 1
DEFAULT_MAX_BYTES = 40_000
MIN_MAX_BYTES = 4_096
TARGET_BUCKETS = 180
DEFAULT_MIN_BUCKET_SECONDS = 10.0
MIN_BUCKET_SECONDS = 5.0
MAX_BUCKET_SECONDS = 600.0  # what TimeIndex.window accepts
MAX_BUCKETS = 720
MIN_REGION_SECONDS = 10.0
MIN_GAP_SECONDS = 2.0
CURVE_POINTS = 60
KEYWORDS = 12
KEYWORD_MIN_LENGTH = 3
SNIPPET_LEVELS = ((40, 60), (20, 30), (10, 15))  # (transcript, screen text) characters per bucket, widest first
_ROW_TEXT = 120  # characters of each snippet kept before they are cut to a level
_CAPS = {"regions": 60, "silence_gaps": 40, "scene_changes": 40}
_EPS = 1e-9

PREPARE_STEPS = 1000
# Where each stage ends on the 0..PREPARE_STEPS progress scale (the model run is by far the slowest).
_PREPARE_WEIGHTS = {"loudness": (0, 50), "silence": (50, 150), "classification": (150, PREPARE_STEPS)}
PREPARABLE = tuple(_PREPARE_WEIGHTS)

STOPWORDS = frozenset(
    [
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "any",
        "can",
        "had",
        "her",
        "was",
        "one",
        "our",
        "out",
        "has",
        "have",
        "him",
        "his",
        "how",
        "its",
        "let",
        "may",
        "new",
        "now",
        "old",
        "see",
        "two",
        "way",
        "who",
        "did",
        "get",
        "got",
        "she",
        "too",
        "use",
        "that",
        "this",
        "with",
        "from",
        "they",
        "them",
        "then",
        "than",
        "there",
        "these",
        "those",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "would",
        "your",
        "about",
        "after",
        "again",
        "also",
        "because",
        "been",
        "before",
        "being",
        "both",
        "could",
        "does",
        "doing",
        "down",
        "each",
        "few",
        "into",
        "just",
        "like",
        "more",
        "most",
        "much",
        "must",
        "only",
        "other",
        "over",
        "said",
        "same",
        "should",
        "some",
        "such",
        "their",
        "very",
        "were",
        "here",
        "yeah",
        "okay",
        "really",
        "gonna",
        "going",
        "know",
        "think",
        "want",
        "right",
        "well",
        "thing",
        "things",
        "going",
        "got",
        "say",
        "says",
        "yes",
    ]
)
_TOKEN = re.compile(r"[a-z][a-z0-9']*")


class MediaReportError(ValueError):
    """A bad `bucket_seconds`, `buckets`, `include` or `max_bytes`. The message says what is allowed."""


def _r(value: float, places: int = 1) -> float:
    return round(float(value), places)


def _compact(doc: dict[str, Any]) -> str:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def dumps(doc: dict[str, Any]) -> str:
    """The document as the surfaces send it: compact JSON (what `max_bytes` bounds)."""
    return _compact(doc)


def encoded_size(doc: dict[str, Any]) -> int:
    """Bytes of the document as compact UTF-8 JSON: what the HTTP API and the CLI's `--json` send, and what
    `max_bytes` bounds."""
    return len(_compact(doc).encode())


# --- preparing the cheap signals ---------------------------------------------------------------


def prepare_signals(
    media: MediaStore,
    asset_id: str,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """Make the cheap signals that are missing: the loudness curve (one streamed decode), the silence scan at
    -35 dB (one ffmpeg pass) and the audio classification (YAMNet, if its model is installed). Never transcribes,
    reads text or finds scenes. Each signal is stored whole when it finishes, so a cancelled run (a `cancel` token
    that fires: `JobCancelledError`) leaves no half-made signal, only the earlier finished ones.

    `on_progress(done, PREPARE_STEPS)` counts thousandths of the work. Returns `{"asset", "prepared": [signals
    made now], "already": [signals that were there], "skipped": [{"signal", "reason"}]}`."""
    asset = assets.get_asset(media, asset_id)
    prepared: list[str] = []
    already: list[str] = []
    skipped: list[dict[str, str]] = []

    def report(stage: str, fraction: float) -> None:
        if on_progress:
            lo, hi = _PREPARE_WEIGHTS[stage]
            on_progress(round(lo + (hi - lo) * min(max(fraction, 0.0), 1.0)), PREPARE_STEPS)

    def check() -> None:
        if cancel is not None:
            cancel.raise_if_cancelled()

    if not asset.has_audio:
        return {
            "asset": asset.id,
            "prepared": [],
            "already": [],
            "skipped": [{"signal": s, "reason": "no audio track"} for s in PREPARABLE],
        }
    report("loudness", 0)
    check()
    waveform = WaveformCache(media, asset.path)
    if waveform.stored_loudness() is None:
        waveform.get_loudness(cancel)
        prepared.append("loudness")
    else:
        already.append("loudness")
    report("loudness", 1)
    check()
    silence = SilenceCache(media, asset.path)
    if silence.stored():
        already.append("silence")
    else:
        silence.get_raw_silences(SILENCE_THRESHOLD_DB, cancel)
        prepared.append("silence")
    report("silence", 1)
    check()
    cache = ClassificationCache(media, asset.path)
    if cache.is_analyzed:
        already.append("classification")
    else:
        try:
            project.run_analysis(
                media,
                asset.path,
                cache,
                cache.get_thresholds(),
                lambda done, total: report("classification", done / max(total, 1)),
                cancel,
            )
            prepared.append("classification")
        except classify.ModelNotFoundError as exc:
            skipped.append({"signal": "classification", "reason": str(exc)})
    report("classification", 1)
    return {"asset": asset.id, "prepared": prepared, "already": already, "skipped": skipped}


# --- the whole-recording structure ----------------------------------------------------------------


def _loudness_summary(index: TimeIndex) -> dict[str, Any] | None:
    values = index.loudness_db
    if values is None:
        return None
    if not values:
        return {"mean_db": None, "p95_db": None, "max_db": None, "curve_seconds": None, "curve": []}
    ranked = sorted(values)
    group = math.ceil(len(values) / CURVE_POINTS)
    step = index.loudness_window_seconds or 1.0
    curve = [round(sum(values[i : i + group]) / len(values[i : i + group])) for i in range(0, len(values), group)]
    return {
        "mean_db": _r(sum(values) / len(values)),
        "p95_db": _r(ranked[math.ceil(0.95 * len(ranked)) - 1]),
        "max_db": _r(ranked[-1]),
        "curve_seconds": _r(group * step),
        "curve": curve,
    }


def _regions(index: TimeIndex, cap: int) -> dict[str, Any] | None:
    found = index.class_regions
    if found is None:
        return None
    seconds: dict[str, float] = {}
    merged: list[list[Any]] = []
    for r in found:
        seconds[r.bucket] = seconds.get(r.bucket, 0.0) + (r.end - r.start)
        if merged and merged[-1][2] == r.bucket and r.start - merged[-1][1] <= 0.5:
            merged[-1][1] = r.end
        else:
            merged.append([r.start, r.end, r.bucket])
    long = [m for m in merged if m[1] - m[0] >= MIN_REGION_SECONDS]
    kept = sorted(sorted(long, key=lambda m: m[0] - m[1])[:cap])  # the longest `cap`, back in time order
    return {
        "min_seconds": MIN_REGION_SECONDS,
        "seconds_by_class": {k: _r(v) for k, v in sorted(seconds.items())},
        "count": len(long),
        "truncated": len(long) - len(kept),
        "items": [[_r(a), _r(b), c] for a, b, c in kept],
    }


def _silence_gaps(index: TimeIndex, cap: int) -> dict[str, Any] | None:
    found = index.silences
    if found is None:
        return None
    long = [(a, b) for a, b in found if b - a >= MIN_GAP_SECONDS]
    kept = sorted(sorted(long, key=lambda g: g[0] - g[1])[:cap])
    return {
        "min_seconds": MIN_GAP_SECONDS,
        "threshold_db": index.silence_threshold_db,
        "count": len(long),
        "truncated": len(long) - len(kept),
        "items": [[_r(a), _r(b)] for a, b in kept],
    }


def _scene_changes(index: TimeIndex, cap: int) -> dict[str, Any] | None:
    found = index.cuts
    if found is None:
        return None
    kept = sorted(sorted(found, key=lambda c: (-c[1], c[0]))[:cap])  # the strongest `cap`, back in time order
    return {
        "count": len(found),
        "truncated": len(found) - len(kept),
        "items": [[_r(t), _r(s, 2)] for t, s in kept],
    }


def _transcript_summary(index: TimeIndex) -> dict[str, Any] | None:
    words = index.words
    if words is None:
        return None
    counts: Counter[str] = Counter()
    for word in words:
        for token in _TOKEN.findall(word.lower()):
            token = token.strip("'")
            if len(token) >= KEYWORD_MIN_LENGTH and token not in STOPWORDS:
                counts[token] += 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:KEYWORDS]
    return {
        "words": len(words),
        "language": index.language,
        "words_per_minute": _r(len(words) / max(index.duration, _EPS) * 60),
        "top_keywords": [[w, n] for w, n in top],
    }


# --- the buckets -------------------------------------------------------------------------------------


def _snippet(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _bucket_rows(index: TimeIndex, size: float, included: set[str]) -> dict[str, list[Any]]:
    """One value per bucket for each column, asked of `TimeIndex.window`. Snippets are kept long here and cut to
    the wanted length when the document is put together."""
    count = max(1, math.ceil(index.duration / size - _EPS))
    names = [
        "audio_class",
        "audio_share",
        "loudness_mean_db",
        "loudness_max_db",
        "silence_fraction",
        "words",
        "words_per_minute",
        "scene_changes",
        "activity_mean",
        "activity_max",
        "screen_text_changed",
        "transcript",
        "screen_text",
    ]
    rows: dict[str, list[Any]] = {name: [] for name in names}
    previous: float | None = None
    for k in range(count):
        a, b = k * size, min(index.duration, (k + 1) * size)
        w = index.window(a, b)
        audio, loud, silence, speech, visual = w["audio"], w["loudness"], w["silence"], w["speech"], w["visual"]
        on = "audio" in included
        rows["audio_class"].append(audio["class"] if on and audio else None)
        rows["audio_share"].append(_r(audio["share"], 2) if on and audio else None)
        on = "loudness" in included and loud is not None and loud["mean_db"] is not None
        rows["loudness_mean_db"].append(_r(loud["mean_db"]) if on else None)
        rows["loudness_max_db"].append(_r(loud["max_db"]) if on else None)
        if "silence" in included and silence:
            covered = sum(i["end"] - i["start"] for i in silence["intervals"])
            rows["silence_fraction"].append(_r(min(1.0, covered / max(b - a, _EPS)), 2))
        else:
            rows["silence_fraction"].append(None)
        on = "speech" in included and speech is not None
        rows["words"].append(speech["word_count"] if on else None)
        rows["words_per_minute"].append(round(speech["words_per_minute"]) if on else None)
        rows["transcript"].append(_snippet(speech["text"], _ROW_TEXT) if on else None)
        scenes_on = "scenes" in included and visual["scene_changes"] is not None
        rows["scene_changes"].append(visual["scene_changes"] if scenes_on else None)
        activity = visual["activity"] if scenes_on else None
        rows["activity_mean"].append(_r(activity["mean"], 2) if activity else None)
        rows["activity_max"].append(_r(activity["max"], 2) if activity else None)
        if "ocr" in included and index.available["ocr"]:
            # The text up at the end of the bucket; "changed" is that differing from the bucket before's.
            state = index.window(max(a, b - 1.0), b)["visual"]["text"]
            key = None if state is None else state["time"]
            changed = key != previous
            previous = key
            rows["screen_text_changed"].append(int(changed))
            first = None if state is None else (state["lines"][0] if state["lines"] else state["text"])
            rows["screen_text"].append(_snippet(first, _ROW_TEXT) if changed and first is not None else None)
        else:
            rows["screen_text_changed"].append(None)
            rows["screen_text"].append(None)
    return rows


def _column(values: list[Any], limit: int) -> list[Any] | None:
    if all(v is None for v in values):
        return None
    return [v if v is None else v[:limit] for v in values]


def _bucket_block(rows: dict[str, list[Any]], size: float, levels: tuple[int, int]) -> dict[str, Any]:
    block: dict[str, Any] = {"seconds": _r(size, 3), "count": len(rows["audio_class"])}
    for name, values in rows.items():
        if name == "transcript":
            block[name] = _column(values, levels[0]) if levels[0] else None
        elif name == "screen_text":
            block[name] = _column(values, levels[1]) if levels[1] else None
        else:
            block[name] = None if all(v is None for v in values) else values
    return block


# --- putting it together ---------------------------------------------------------------------------------


def _asset_block(asset: assets.Asset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "path": asset.path,
        "duration": _r(asset.duration, 3),
        "size": asset.size,
        "has_audio": asset.has_audio,
        "has_video": asset.has_video,
        "streams": [s for s, on in (("video", asset.has_video), ("audio", asset.has_audio)) if on],
        "width": asset.width,
        "height": asset.height,
        "fps": None if asset.fps is None else _r(asset.fps, 3),
    }


def _parse_include(include: Iterable[str] | str | None) -> set[str]:
    if include is None:
        return set(SIGNALS)
    names = [n.strip() for n in include.split(",")] if isinstance(include, str) else [str(n).strip() for n in include]
    names = [n for n in names if n]
    unknown = [n for n in names if n not in SIGNALS]
    if unknown or not names:
        raise MediaReportError(f"include must name some of {', '.join(SIGNALS)} (got {', '.join(unknown) or 'none'})")
    return set(names)


def parse_include(include: Iterable[str] | str | None) -> list[str] | None:
    """`include` as the adapters get it (a comma separated string or a list) to a list of signal names, or None for
    all of them. `MediaReportError` for an unknown name."""
    return None if include is None else [s for s in SIGNALS if s in _parse_include(include)]


def _default_bucket(duration: float) -> float:
    return float(max(DEFAULT_MIN_BUCKET_SECONDS, math.ceil(duration / TARGET_BUCKETS)))


def _requested_bucket(duration: float, bucket_seconds: float | None, buckets: int | None) -> float:
    if bucket_seconds is not None and buckets is not None:
        raise MediaReportError("give bucket_seconds or buckets, not both")
    if bucket_seconds is not None:
        if isinstance(bucket_seconds, bool) or not isinstance(bucket_seconds, int | float):
            raise MediaReportError("bucket_seconds must be a number of seconds")
        if not MIN_BUCKET_SECONDS <= bucket_seconds <= MAX_BUCKET_SECONDS:
            raise MediaReportError(
                f"bucket_seconds must be from {MIN_BUCKET_SECONDS:g} to {MAX_BUCKET_SECONDS:g} (got {bucket_seconds:g})"
            )
        size = float(bucket_seconds)
    elif buckets is not None:
        if isinstance(buckets, bool) or not isinstance(buckets, int) or not 1 <= buckets <= MAX_BUCKETS:
            raise MediaReportError(f"buckets must be a whole number from 1 to {MAX_BUCKETS}")
        size = float(min(MAX_BUCKET_SECONDS, max(MIN_BUCKET_SECONDS, math.ceil(duration / buckets))))
    else:
        size = _default_bucket(duration)
    size = min(size, MAX_BUCKET_SECONDS)
    count = math.ceil(duration / size - _EPS)
    if count > MAX_BUCKETS:
        raise MediaReportError(
            f"{count} buckets is too many (at most {MAX_BUCKETS}): use bucket_seconds of at least "
            f"{math.ceil(duration / MAX_BUCKETS)}"
        )
    return size


def media_report(
    media: MediaStore,
    asset_id: str,
    *,
    bucket_seconds: float | None = None,
    buckets: int | None = None,
    include: Iterable[str] | str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    prepare: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """Every stored signal of the asset as one document that is at most `max_bytes` (default `DEFAULT_MAX_BYTES`)
    as compact JSON; see the module docstring for the shape and for how it shrinks. Read-only unless `prepare` is
    true, which first makes the missing cheap signals (`prepare_signals`). `AssetNotFoundError` for an unknown
    asset, `MediaReportError` for a bad argument."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < MIN_MAX_BYTES:
        raise MediaReportError(f"max_bytes must be a whole number of at least {MIN_MAX_BYTES}")
    included = _parse_include(include)
    asset = assets.get_asset(media, asset_id)
    requested = _requested_bucket(asset.duration, bucket_seconds, buckets)
    prepared = prepare_signals(media, asset.id, on_progress=on_progress, cancel=cancel) if prepare else None
    index = TimeIndex.load(media, asset.id)
    if cancel is not None:
        cancel.raise_if_cancelled()

    available = {s: index.available[s] for s in SIGNALS}
    head = {
        "version": VERSION,
        "asset": _asset_block(index.asset),
        "signals": {
            "available": available,
            "missing": index.missing,
            "excluded": [s for s in SIGNALS if s not in included],
        },
        "prepared": prepared,
    }

    def assemble(
        size: float, rows: dict[str, list[Any]] | None, levels: tuple[int, int], caps: dict[str, int]
    ) -> dict[str, Any]:
        structure = {
            "loudness": _loudness_summary(index) if "loudness" in included else None,
            "regions": _regions(index, caps["regions"]) if "audio" in included else None,
            "silence_gaps": _silence_gaps(index, caps["silence_gaps"]) if "silence" in included else None,
            "scene_changes": _scene_changes(index, caps["scene_changes"]) if "scenes" in included else None,
            "transcript": _transcript_summary(index) if "speech" in included else None,
        }
        block = None if rows is None else _bucket_block(rows, size, levels)
        dropped = []
        if rows is not None:
            for name in ("transcript", "screen_text"):
                if block[name] is None and any(v is not None for v in rows[name]):
                    dropped.append(name)
        if rows is None:
            dropped.append("buckets")
        shrunk = None
        if rows is not None and levels != SNIPPET_LEVELS[0] and levels != (0, 0):
            shrunk = {"transcript_chars": levels[0], "screen_text_chars": levels[1]}
        truncated = [n for n in _CAPS if structure[n] and structure[n]["truncated"]]
        notices = {
            "coarsened": None
            if size == requested
            else {"requested_bucket_seconds": _r(requested, 3), "bucket_seconds": _r(size, 3), "reason": "max_bytes"},
            "shrunk": shrunk,
            "dropped": dropped,
            "truncated": truncated,
        }
        return {
            **head,
            "limits": {"max_bytes": max_bytes},
            "notices": notices,
            "buckets": block,
            **structure,
        }

    # The degradation order, each step tried only when the one before still does not fit: shorter snippets, no
    # snippets, coarser buckets (doubling up to 600 s, snippets restored), shorter lists, no buckets, no structure.
    size = requested
    full_caps = dict(_CAPS)
    while True:
        rows = _bucket_rows(index, size, included)
        for levels in (*SNIPPET_LEVELS, (0, 0)):
            doc = assemble(size, rows, levels, full_caps)
            if encoded_size(doc) <= max_bytes:
                return doc
        if size >= MAX_BUCKET_SECONDS or math.ceil(index.duration / size - _EPS) <= 1:
            break
        size = min(MAX_BUCKET_SECONDS, size * 2)
    for divisor in (2, 4, 8, 16, 0):
        caps = {k: v // divisor if divisor else 0 for k, v in _CAPS.items()}
        doc = assemble(size, rows, (0, 0), caps)
        if encoded_size(doc) <= max_bytes:
            return doc
    doc = assemble(size, None, (0, 0), {k: 0 for k in _CAPS})
    if encoded_size(doc) <= max_bytes:
        return doc
    return {  # only for a budget below the signal hints themselves: the document stays inside it regardless
        "version": VERSION,
        "asset": {"id": index.asset.id},
        "signals": {"available": available, "missing": [], "excluded": head["signals"]["excluded"]},
        "prepared": None,
        "limits": {"max_bytes": max_bytes},
        "notices": {"coarsened": None, "shrunk": None, "dropped": ["everything"], "truncated": []},
        "buckets": None,
        "loudness": None,
        "regions": None,
        "silence_gaps": None,
        "scene_changes": None,
        "transcript": None,
    }
