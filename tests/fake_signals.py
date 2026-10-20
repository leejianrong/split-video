"""Every stored signal planted at known times, through the engines' own save paths (SPL-100).

`plant(media, path, skip=(), duration=300.0, dense=False)` registers `path` and stores, for the asset:

  speech        10 words from 98.0 s (`SAID`), "welcome" first; with `dense` a word every 0.4 s everywhere else
  audio         music to 96 s, speech to 200 s, applause_crowd 200-210 s, music after (YAMNet's 0.48 s frames)
  loudness      1 s dB values: -40 baseline, -22 while speaking, a peak of -3 at 205 s (the applause)
  silence       raw scan at -35 dB: 96.0-97.5 and 199.0-200.0 (kept), 210.0-210.3 (too short to report)
  scenes        cuts at 20 s and 150 s (the 150 s one is the strong one); activity spikes there
  ocr           "intro slide" 0-140 s, then `ON_SCREEN` from 140 s to the end
  with `dense`: also a cut every 20 s, a text change every 30 s and a silence every 60 s (for long recordings)

`skip` names signals to leave out: "audio", "loudness", "silence", "speech", "scenes", "ocr".
"""

from __future__ import annotations

import math

from fake_ocr import store_ocr

from split_video import assets, scenes, transcribe
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache
from split_video.ffmpeg import MediaInfo
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import InMemoryMediaStore

DURATION = 300.0
SAID = ["welcome", "everybody", "to", "the", "live", "stream", "tonight", "we", "start", "now"]
SAID_FROM = 98.0
SAID_EVERY = 0.5
ON_SCREEN = "def main():\n    run_stream()"
PEAK_AT = 205.0
CUT_AT = 150.0
BUCKETS = list(classify.BUCKET_LABELS)


def make_store(kind: str, tmp_path, *, duration: float = DURATION, path: str = "talk.mp4"):
    """An in-memory or a local store holding a stand-in `path` that probes as `duration` seconds of video + audio."""
    info = MediaInfo(duration=duration, width=1280, height=720, has_audio=True, fps=5.0)
    if kind == "memory":
        store = InMemoryMediaStore()
        store.add(path, b"x", info=info)
        return store
    store = LocalMediaStore(tmp_path)
    (tmp_path / path).write_bytes(b"not really a video")
    store.probe = lambda media_id: info
    return store


def _class_at(t: float) -> str:
    if t < 96.0:
        return "music"
    if t < 200.0:
        return "speech"
    if t < 210.0:
        return "applause_crowd"
    return "music"


def _words(duration: float, dense: bool) -> list[tuple[float, str]]:
    said = [(SAID_FROM + SAID_EVERY * k, w) for k, w in enumerate(SAID)]
    if not dense:
        return said
    lo, hi = SAID_FROM - 0.5, SAID_FROM + SAID_EVERY * len(SAID) + 0.5
    rest = [(round(0.4 * k, 3), f"w{k % 97}") for k in range(int(duration / 0.4) - 1)]
    return sorted([w for w in rest if not lo <= w[0] < hi] + said)


def _transcript(asset, duration: float, dense: bool) -> dict:
    words = _words(duration, dense)
    segments = []
    for first in range(0, len(words), 12):
        stored = [
            {"start": t, "end": round(t + 0.4, 3), "word": w, "probability": 0.9} for t, w in words[first : first + 12]
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


def _loudness(duration: float) -> list[float]:
    out = []
    for k in range(math.ceil(duration)):
        if k == int(PEAK_AT):
            out.append(-3.0)
        elif 98 <= k < 104:
            out.append(-22.0)
        else:
            out.append(-40.0)
    return out


def plant(media, path: str = "talk.mp4", *, skip=(), duration: float = DURATION, dense: bool = False):
    asset = assets.register_path(media, path)
    skip = set(skip)
    if "speech" not in skip:
        transcribe.save(media, asset, _transcript(asset, duration, dense))
    if "audio" not in skip:
        frames = []
        for i in range(math.ceil(duration * classify.SAMPLE_RATE / classify.HOP_SAMPLES)):
            winner = _class_at(i * classify.HOP_SAMPLES / classify.SAMPLE_RATE)
            if dense and int(i * 0.48 // 30) % 2:
                winner = "singing"
            frames.append({b: (0.9 if b == winner else 0.05) for b in BUCKETS})
        thresholds = dict(classify.DEFAULT_THRESHOLDS)
        regions = classify.rethreshold(frames, thresholds, duration)
        ClassificationCache(media, asset.path).store_result(frames, duration, regions)
    if "loudness" not in skip:
        media.save_cache(
            asset.path,
            "waveform",
            {"buckets": [[-0.1, 0.1]] * 10, "loudness": {"window_seconds": 1.0, "db": _loudness(duration)}},
        )
    if "silence" not in skip:
        raw = [[96.0, 97.5], [199.0, 200.0], [210.0, 210.3]]
        if dense:
            raw += [[float(t), t + 1.0] for t in range(300, int(duration), 60)]
        media.save_cache(asset.path, "silence", {"-35.0": raw, "-50.0": [[10.0, 11.0]]})
    if "scenes" not in skip:
        cuts = [
            {"time": 20.0, "score": 0.4, "kind": "cut", "changed": 0.7},
            {"time": CUT_AT, "score": 0.62, "kind": "cut", "changed": 0.9},
        ]
        if dense:
            extra = [
                {"time": float(t), "score": 0.3, "kind": "cut", "changed": 0.5}
                for t in range(40, int(duration), 20)
                if t != int(CUT_AT)
            ]
            cuts = sorted(cuts + extra, key=lambda c: c["time"])
        activity = [0.9 if k in (20, int(CUT_AT)) else 0.1 for k in range(math.ceil(duration))]
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
            "scenes": cuts,
            "activity": activity,
        }
        media.save_cache(asset.path, scenes.CACHE_NAME, document)
        media.save_cache(
            asset.path,
            scenes.INFO_NAME,
            {k: v for k, v in document.items() if k not in ("scenes", "activity")}
            | {"scenes": len(cuts), "activity_buckets": len(activity)},
        )
    if "ocr" not in skip:
        screens = [(0.0, "intro slide"), (140.0, ON_SCREEN)]
        if dense:
            screens += [(float(t), f"def handler_{t}():\n    return {t}") for t in range(300, int(duration), 30)]
        store_ocr(media, asset, screens, duration=duration)
    return asset
