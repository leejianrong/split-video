"""A stand-in for `faster_whisper.WhisperModel`, so transcription is tested without the optional dependency.

The "speech" is tone bursts: word k is a 0.4 s sine at `FREQS[k % len(FREQS)]` Hz, separated by digital silence.
The fake finds the bursts in the audio it is given and reports one word per burst, with the burst's times
(relative to the audio it got, as the real model does) and a word named after its frequency. So a test knows
exactly which words and times a correct pipeline must produce, however the audio was cut into windows.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass, field

import numpy as np

SAMPLE_RATE = 16000
FREQS = (300, 500, 700, 900, 1100)
BURST = 0.4


def word_name(freq: float) -> str:
    return f"w{round(freq / 100.0) * 100}"


@dataclass
class Word:
    start: float
    end: float
    word: str
    probability: float = 0.9


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Info:
    language: str = "en"
    language_probability: float = 0.99


def speech(starts: list[float], total: float) -> tuple[np.ndarray, list[tuple[str, float, float]]]:
    """int16 audio of `total` seconds with a burst at each of `starts`, and the `(word, start, end)` it holds."""
    audio = np.zeros(int(total * SAMPLE_RATE), dtype=np.float64)
    truth = []
    t = np.arange(int(BURST * SAMPLE_RATE)) / SAMPLE_RATE
    for k, start in enumerate(starts):
        freq = FREQS[k % len(FREQS)]
        i = int(start * SAMPLE_RATE)
        audio[i : i + len(t)] = 0.5 * np.sin(2 * np.pi * freq * t)
        truth.append((word_name(freq), start, start + BURST))
    return (audio * 32767).astype("<i2"), truth


def write_wav(path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm.tobytes())


class FakeModel:
    """Records every `transcribe` call; `per_segment` words go into each segment it yields."""

    def __init__(self, per_segment: int = 3, language: str = "en", on_segment=None) -> None:
        self.calls: list[dict] = []
        self.per_segment = per_segment
        self.language = language
        self.on_segment = on_segment  # called with the index of each segment as it is yielded

    def transcribe(self, audio, **kwargs):
        self.calls.append({"samples": len(audio), "dtype": str(audio.dtype), **kwargs})
        # A sine crosses zero all the time, so close gaps shorter than 50 ms before looking for the edges.
        loud = np.convolve((np.abs(audio) > 0.01).astype(np.float32), np.ones(801, dtype=np.float32), "same") > 0
        padded = np.concatenate([[0], loud.astype(np.int8), [0]])
        edges = np.flatnonzero(np.diff(padded))
        starts, ends = edges[::2] + 400, edges[1::2] - 400
        words = []
        for s, e in zip(starts, ends, strict=False):
            burst = audio[s:e]
            spectrum = np.abs(np.fft.rfft(burst))
            freq = np.argmax(spectrum) * SAMPLE_RATE / len(burst)
            words.append(Word(s / SAMPLE_RATE, e / SAMPLE_RATE, " " + word_name(freq)))
        segments = [
            Segment(group[0].start, group[-1].end, "".join(w.word for w in group), group)
            for group in (words[i : i + self.per_segment] for i in range(0, len(words), self.per_segment))
        ]

        def generate():
            for index, segment in enumerate(segments):
                yield segment
                if self.on_segment is not None:
                    self.on_segment(index)

        return generate(), Info(language=self.language)
