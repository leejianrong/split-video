"""Long recordings with KNOWN planted peaks and decoys, stored through the engines' own save paths (SPL-48).

`gig(media)`: 3 h of music at about -25 dB with a silence between songs every 300 s (150, 450, ...), and
    PEAKS   applause + loud music (-9 dB) at 1200-1230, 5400-5430, 9000-9040, each with a silence in front (5 s) and after
    DECOYS  steady banter 3000-3300 (2.5 words/s), a quiet stretch 4000-4100, a speech burst (4 words/s) 7000-7030
            and a short moderate applause 2000-2008 (-20 dB)
`stream(media)`: 3 h of a coding stream: steady commentary (2 words/s, a sentence every 12 words), a screen change every
    60 s, screen activity 0.1, and
    PEAKS   a speech burst (4 words/s) + 6 screen changes + activity 0.9 + on-screen "FAILED ..." at 2000-2040, 7000-7050,
            each with a 5 s silence in front and after
    DECOYS  a quiet stretch 4000-4200 (no words, -60 dB), a loud shout with no screen change 5000-5020 (-5 dB)
`mixed(media)`: 1 h with the gig peak at 1000-1030 (applause + loud music) AND the stream peak at 2000-2040 (speech
    burst + screen changes + FAILED on screen), for the preset and weight tests.

Every function takes `skip` (names of signals to leave out: audio, loudness, silence, speech, scenes, ocr) and returns
the asset.
"""

from __future__ import annotations

import math

import numpy as np
from fake_ocr import store_ocr
from fake_signals import make_store  # noqa: F401  (re-exported for the tests)

from split_video import assets, scenes, transcribe
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache

GIG_PEAKS = [(1200.0, 1230.0), (5400.0, 5430.0), (9000.0, 9040.0)]
STREAM_PEAKS = [(2000.0, 2040.0), (7000.0, 7050.0)]
FAILED = "FAILED tests/unit/test_render.py::test_overlay_order"
BUCKETS = list(classify.BUCKET_LABELS)


def _words(runs):
    """`runs`: [(start, end, per_second, sentence_every_or_None)] -> [(time, word)]."""
    out = []
    for a, b, rate, every in runs:
        t, k = a, 0
        while t + 0.2 < b:
            out.append((round(t, 3), f"w{k % 53}"))
            k += 1
            t += 1.0 / rate
            if every and k % every == 0:
                t += 0.7
    return out


def _transcript(asset, duration, words, word_len=0.2):
    segments = []
    for first in range(0, len(words), 12):
        stored = [
            {"start": t, "end": round(t + word_len, 3), "word": w, "probability": 0.9}
            for t, w in words[first : first + 12]
        ]
        segments.append(
            {
                "start": stored[0]["start"],
                "end": stored[-1]["end"],
                "text": " ".join(w["word"] for w in stored),
                "words": stored,
            }
        )
    return {
        "version": transcribe.VERSION,
        "asset": asset.id,
        "model": "base.en",
        "language": "en",
        "language_probability": 1.0,
        "duration": duration,
        "vad": True,
        "segments": segments,
    }


def _store(
    media,
    path,
    duration,
    *,
    classes,
    loud,
    silences,
    words,
    cuts,
    activity,
    screens,
    skip,
    word_len=0.2,
):
    """`classes`: [(a, b, class)] over a default of the first entry; `loud`: [(a, b, db)] over the first entry's level."""
    skip = set(skip)
    asset = assets.register_path(media, path)
    n = math.ceil(duration)
    if "speech" not in skip:
        transcribe.save(media, asset, _transcript(asset, duration, words, word_len))
    if "audio" not in skip:
        count = math.ceil(duration * classify.SAMPLE_RATE / classify.HOP_SAMPLES)
        times = np.arange(count) * classify.HOP_SAMPLES / classify.SAMPLE_RATE
        winner = np.full(count, BUCKETS.index(classes[0][2]))
        for a, b, name in classes[1:]:
            winner[(times >= a) & (times < b)] = BUCKETS.index(name)
        frames = [{b: (0.9 if k == w else 0.05) for k, b in enumerate(BUCKETS)} for w in winner]
        regions = classify.rethreshold(frames, dict(classify.DEFAULT_THRESHOLDS), duration)
        ClassificationCache(media, asset.path).store_result(frames, duration, regions)
    if "loudness" not in skip:
        rng = np.random.default_rng(7)
        db = loud[0][2] + rng.uniform(-3, 3, n)
        for a, b, level in loud[1:]:
            db[int(a) : int(b)] = level + rng.uniform(-1, 1, int(b) - int(a))
        media.save_cache(
            asset.path,
            "waveform",
            {
                "buckets": [[-0.1, 0.1]] * 10,
                "loudness": {"window_seconds": 1.0, "db": [round(float(v), 1) for v in db]},
            },
        )
    if "silence" not in skip:
        media.save_cache(asset.path, "silence", {"-35.0": [list(map(float, s)) for s in silences]})
    if "scenes" not in skip:
        act = np.full(n, activity[0][2])
        for a, b, level in activity[1:]:
            act[int(a) : int(b)] = level
        document = {
            "version": scenes.VERSION,
            "asset": asset.id,
            "source": "original",
            "duration": duration,
            "threshold": 0.15,
            "min_gap": 1.0,
            "screen_threshold": scenes.DEFAULT_SCREEN_THRESHOLD,
            "sample_fps": 5.0,
            "activity_interval": 1.0,
            "scenes": [{"time": float(t), "score": 0.5, "kind": k, "changed": 0.6} for t, k in sorted(cuts)],
            "activity": [round(float(v), 3) for v in act],
        }
        media.save_cache(asset.path, scenes.CACHE_NAME, document)
        media.save_cache(
            asset.path,
            scenes.INFO_NAME,
            {k: v for k, v in document.items() if k not in ("scenes", "activity")}
            | {"scenes": len(cuts), "activity_buckets": n},
        )
    if "ocr" not in skip:
        store_ocr(media, asset, screens, duration=duration)
    return asset


def _peak_silences(peaks):
    out = []
    for a, b in peaks:
        out += [(a - 5.0, a), (b, b + 5.0)]
    return out


def gig(media, path="gig.mp4", *, skip=(), duration=10_800.0):
    classes = [(0.0, duration, "music")]
    loud = [(0.0, duration, -25.0), (2000, 2008, -20.0), (3000, 3300, -24.0), (4000, 4100, -60.0), (7000, 7030, -24.0)]
    silences = [(t - 3.0, float(t)) for t in range(150, int(duration), 300)]
    silences = [s for s in silences if not any(a - 6 < s[0] < b + 6 for a, b in GIG_PEAKS)]
    silences += _peak_silences(GIG_PEAKS) + [(4000.0, 4100.0)]
    classes += [(2000.0, 2008.0, "applause_crowd"), (3000.0, 3300.0, "speech"), (4000.0, 4100.0, "silence_other")]
    classes += [(7000.0, 7030.0, "speech")]
    words = _words([(3000.0, 3300.0, 2.5, 12), (7000.0, 7030.0, 4.0, None)])
    for a, b in GIG_PEAKS:
        classes.append((a, b, "applause_crowd"))
        loud.append((a, b, -9.0))
    for a, b in _peak_silences(GIG_PEAKS):
        classes.append((a, b, "silence_other"))
        loud.append((a, b, -60.0))
    return _store(
        media,
        path,
        duration,
        classes=classes,
        loud=loud,
        silences=sorted(silences),
        words=words,
        cuts=[(float(t) + 7.0, "cut") for t in range(600, int(duration), 600)],
        activity=[(0, 0, 0.1)],
        screens=[(0.0, "stage camera")],
        skip=skip,
    )


def stream(media, path="stream.mp4", *, skip=(), duration=10_800.0):
    quiet = (4000.0, 4200.0)
    runs = [(0.0, quiet[0], 2.0, 12), (quiet[1], duration, 2.0, 12)]
    classes = [(0.0, duration, "speech"), (*quiet, "silence_other")]
    loud = [(0.0, duration, -30.0), (*quiet, -60.0), (5000, 5020, -5.0)]
    silences = [quiet] + _peak_silences(STREAM_PEAKS)
    cuts = [(float(t), "cut") for t in range(60, int(duration), 60)]
    activity = [(0, 0, 0.1)]
    screens = [(0.0, "$ uv run pytest")]
    cut_set = {t for t, _ in cuts}
    for a, b in STREAM_PEAKS:
        runs = [r for r in runs if not (r[0] < b and r[1] > a - 5)]  # no steady speech under a peak
        loud.append((a, b, -20.0))
        activity.append((a, b, 0.9))
        cut_set |= {a + 3.0 + 6.0 * k for k in range(6)}
        screens += [(a + 3.0, FAILED), (b - 3.0, "$ uv run pytest tests/unit/test_render.py")]
        classes += [(a - 5, a, "silence_other"), (b, b + 5, "silence_other")]
        loud += [(a - 5, a, -60.0), (b, b + 5, -60.0)]
    # the steady speech runs have to stop around each peak, in 5 s of silence
    base = [(0.0, duration)]
    cuts_peak = [(t, "screen") for t in sorted(cut_set - {t for t, _ in cuts})]
    spans = base
    for a, b in [quiet] + STREAM_PEAKS:
        spans = [
            piece
            for s0, s1 in spans
            for piece in ((s0, min(s1, a - 5.0)), (max(s0, b + 5.0), s1))
            if piece[1] > piece[0]
        ]
    runs = [(s0, s1, 2.0, 12) for s0, s1 in spans]
    runs += [(a, b, 4.0, None) for a, b in STREAM_PEAKS]
    screens.sort()
    return _store(
        media,
        path,
        duration,
        classes=classes,
        loud=loud,
        silences=sorted(silences),
        words=_words(sorted(runs)),
        cuts=cuts + cuts_peak,
        activity=activity,
        screens=screens,
        skip=skip,
    )


def mixed(media, path="mixed.mp4", *, skip=(), duration=3600.0):
    gig_peak, burst = (1000.0, 1030.0), (2000.0, 2040.0)
    classes = [
        (0.0, duration, "speech"),
        (*gig_peak, "applause_crowd"),
        (gig_peak[0] - 5, gig_peak[0], "silence_other"),
    ]
    classes += [(gig_peak[1], gig_peak[1] + 5, "silence_other"), (burst[0] - 5, burst[0], "silence_other")]
    classes += [(burst[1], burst[1] + 5, "silence_other")]
    loud = [(0.0, duration, -30.0), (*gig_peak, -9.0), (*burst, -22.0)]
    for a, b in (
        (gig_peak[0] - 5, gig_peak[0]),
        (gig_peak[1], gig_peak[1] + 5),
        (burst[0] - 5, burst[0]),
        (burst[1], burst[1] + 5),
    ):
        loud.append((a, b, -60.0))
    silences = _peak_silences([gig_peak, burst])
    spans = [(0.0, duration)]
    for a, b in (gig_peak, burst):
        spans = [p for s0, s1 in spans for p in ((s0, min(s1, a - 5.0)), (max(s0, b + 5.0), s1)) if p[1] > p[0]]
    runs = [(s0, s1, 2.0, 12) for s0, s1 in spans] + [(*burst, 4.0, None)]
    cuts = [(float(t), "cut") for t in range(60, int(duration), 60)] + [
        (burst[0] + 3.0 + 6.0 * k, "screen") for k in range(6)
    ]
    return _store(
        media,
        path,
        duration,
        classes=classes,
        loud=loud,
        silences=sorted(silences),
        words=_words(sorted(runs)),
        cuts=cuts,
        activity=[(0, 0, 0.1), (*burst, 0.9)],
        screens=[(0.0, "$ uv run pytest"), (burst[0] + 3.0, FAILED), (burst[1] - 3.0, "$ git status")],
        skip=skip,
    )
