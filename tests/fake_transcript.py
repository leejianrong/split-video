"""Synthetic transcripts in the stored format (see transcribe.py), saved through `transcribe.save`, so the search
and paging tests (and the long-recording measurements) read exactly what a real transcription would leave."""

from __future__ import annotations

from split_video import assets, transcribe
from split_video.ffmpeg import MediaInfo
from split_video.media_store import InMemoryMediaStore

VOCAB = [
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
]


def make_document(
    asset_id: str,
    *,
    words: int = 36_000,
    per_segment: int = 12,
    word_seconds: float = 0.3,
    planted: dict[int, list[str]] | None = None,
) -> dict:
    """A transcript of `words` words, one every `word_seconds`, in segments of `per_segment` words with a 1 s pause
    between segments. `planted` maps a word index to the words to put there (a known phrase at a known place);
    everything else cycles through `VOCAB`."""
    texts = [VOCAB[(i * 7 + i // 16) % len(VOCAB)] for i in range(words)]
    for index, phrase in (planted or {}).items():
        texts[index : index + len(phrase)] = phrase
    segments = []
    t = 0.0
    for first in range(0, words, per_segment):
        stored = []
        for text in texts[first : first + per_segment]:
            stored.append(
                {"start": round(t, 3), "end": round(t + word_seconds * 0.9, 3), "word": text, "probability": 0.9}
            )
            t += word_seconds
        t += 1.0
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
        "asset": asset_id,
        "model": "base.en",
        "language": "en",
        "language_probability": 1.0,
        "duration": round(t, 3),
        "vad": True,
        "segments": segments,
    }


def store_transcript(media, path: str = "talk.wav", **kwargs) -> tuple[assets.Asset, dict]:
    """Register `path` (adding a stand-in file to an in-memory store) and store a synthetic transcript for it."""
    if isinstance(media, InMemoryMediaStore) and not media.exists(path):
        media.add(path, b"x", info=MediaInfo(duration=11_000.0, width=None, height=None, has_audio=True))
    asset = assets.register_path(media, path)
    document = make_document(asset.id, **kwargs)
    transcribe.save(media, asset, document)
    return asset, document


def document_from_words(asset_id: str, words: list[tuple[float, float, str]]) -> dict:
    """A transcript with exactly these `(start, end, word)` words, in one segment per sentence-less run of 8."""
    stored = [{"start": a, "end": b, "word": w, "probability": 0.9} for a, b, w in words]
    segments = [
        {
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
            "text": " ".join(w["word"] for w in chunk),
            "words": chunk,
        }
        for chunk in (stored[i : i + 8] for i in range(0, len(stored), 8))
    ]
    return {
        "version": transcribe.VERSION,
        "asset": asset_id,
        "model": "base.en",
        "language": "en",
        "language_probability": 1.0,
        "duration": stored[-1]["end"] if stored else 0.0,
        "vad": True,
        "segments": segments,
    }
