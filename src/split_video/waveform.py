"""Peak min/max bucketing of raw PCM audio samples for the timeline's waveform overlay."""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

DEFAULT_BUCKET_COUNT = 2000


@dataclass(frozen=True)
class WaveformPeaks:
    """Peak envelope of an audio track, as (min, max) sample pairs per bucket.

    Buckets are contiguous, equal-length spans of the source sample array
    (not of wall-clock time directly), so bucket `i` covers a `1/len(buckets)`
    fraction of the track regardless of its actual sample rate or duration.
    """

    buckets: list[tuple[float, float]]


def compute_peaks(pcm_s16le: bytes, bucket_count: int = DEFAULT_BUCKET_COUNT) -> WaveformPeaks:
    """Bucket raw mono little-endian 16-bit PCM samples into peak (min, max) pairs.

    Args:
        pcm_s16le: Raw little-endian signed 16-bit PCM audio, one channel.
        bucket_count: Number of (min, max) buckets to produce, evenly spanning
            the sample array. Clamped down to the sample count when there are
            fewer samples than requested buckets.

    Returns:
        The bucketed peaks, each value normalized to [-1.0, 1.0].
    """
    samples = np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32) / 32768.0
    if samples.size == 0:
        return WaveformPeaks(buckets=[])

    bucket_count = min(bucket_count, samples.size)
    edges = np.linspace(0, samples.size, bucket_count + 1).astype(np.int64)

    buckets = [(float(samples[s:e].min()), float(samples[s:e].max())) for s, e in itertools.pairwise(edges)]
    return WaveformPeaks(buckets=buckets)


# --- streaming ----------------------------------------------------------------------------------
#
# `compute_peaks` buckets by exact fractions of the total sample count, which a stream only knows at
# the end. `PeakAccumulator` is told the expected count up front (from the probed duration) and keeps
# the samples around each *expected* bucket edge raw (`edge_margin` on each side) while it reduces the
# stretches between them to small blocks. At the end the real count fixes the real edges, and each edge
# lands inside a raw stretch whenever the real count is within `edge_margin - 1` samples of the
# expected one. The result is then bit-identical to `compute_peaks`. If the guess is further off (a
# container duration that disagrees with the audio), an edge can fall inside a block and that block is
# assigned to the bucket holding its middle: peaks are then off by at most one block (`BLOCK_SAMPLES`
# samples) at each bucket boundary, which is invisible at 2000 buckets. Memory is about
# `bucket_count * 2 * edge_margin` raw int16 samples plus `total / BLOCK_SAMPLES` blocks of 14 bytes (3 h
# at 8 kHz: about 4 MB and 5 MB), and the whole file raw only when it is so short that the margins cover
# it. With no expected count everything is kept raw (the old memory profile).

EDGE_MARGIN_SAMPLES = 512
BLOCK_SAMPLES = 256
LOUDNESS_WINDOW_SECONDS = 1.0
LOUDNESS_FLOOR_DB = -100.0


class PeakAccumulator:
    def __init__(
        self,
        expected_samples: int | None,
        bucket_count: int = DEFAULT_BUCKET_COUNT,
        edge_margin: int = EDGE_MARGIN_SAMPLES,
        block_samples: int = BLOCK_SAMPLES,
    ) -> None:
        self._bucket_count = bucket_count
        self._block = block_samples
        self._offset = 0
        self._starts: list[np.ndarray] = []
        self._lengths: list[np.ndarray] = []
        self._mins: list[np.ndarray] = []
        self._maxs: list[np.ndarray] = []
        self._raw_starts: list[int] = []
        self._raw_data: list[np.ndarray] = []
        self._keep_lo: np.ndarray | None = None  # None: keep everything raw
        self._keep_hi: np.ndarray | None = None
        if expected_samples is None or expected_samples <= 0:
            return
        buckets = min(bucket_count, expected_samples)
        edges = np.linspace(0, expected_samples, buckets + 1).astype(np.int64)
        lo = np.maximum(edges - edge_margin, 0)
        hi = edges + edge_margin
        # Merge overlapping raw stretches (all of them, for a short file).
        merged_lo: list[int] = []
        merged_hi: list[int] = []
        for a, b in zip(lo.tolist(), hi.tolist(), strict=True):
            if merged_hi and a <= merged_hi[-1]:
                merged_hi[-1] = max(merged_hi[-1], b)
            else:
                merged_lo.append(a)
                merged_hi.append(b)
        self._keep_lo = np.asarray(merged_lo, dtype=np.int64)
        self._keep_hi = np.asarray(merged_hi, dtype=np.int64)

    def feed(self, samples: np.ndarray) -> None:
        n = samples.size
        if n == 0:
            return
        base = self._offset
        self._offset += n
        if self._keep_lo is None or self._keep_hi is None:
            self._raw(samples, base)
            return
        end = base + n
        cursor = base
        first = int(np.searchsorted(self._keep_hi, base, side="right"))  # first stretch ending after base
        for j in range(first, self._keep_lo.size):
            a, b = int(self._keep_lo[j]), int(self._keep_hi[j])
            if a >= end:
                break
            if a > cursor:
                self._blocks(samples[cursor - base : a - base], cursor)
                cursor = a
            stop = min(b, end)
            self._raw(samples[cursor - base : stop - base], cursor)
            cursor = stop
        if cursor < end:
            self._blocks(samples[cursor - base :], cursor)

    def _raw(self, samples: np.ndarray, start: int) -> None:
        if samples.size:
            # A copy: a view would keep the whole decoded chunk alive.
            self._raw_starts.append(start)
            self._raw_data.append(samples.copy())

    def _blocks(self, samples: np.ndarray, start: int) -> None:
        if samples.size:
            idx = np.arange(0, samples.size, self._block)
            self._starts.append(idx.astype(np.int64) + start)
            self._lengths.append(np.diff(np.append(idx, samples.size)).astype(np.uint16))
            self._mins.append(np.minimum.reduceat(samples, idx))
            self._maxs.append(np.maximum.reduceat(samples, idx))

    @property
    def total_samples(self) -> int:
        return self._offset

    def finish(self) -> WaveformPeaks:
        total = self._offset
        if total == 0:
            return WaveformPeaks(buckets=[])
        count = min(self._bucket_count, total)
        edges = np.linspace(0, total, count + 1).astype(np.int64)
        lo = np.full(count, 32767, dtype=np.int16)
        hi = np.full(count, -32768, dtype=np.int16)

        def assign(positions: np.ndarray, lows: np.ndarray, highs: np.ndarray) -> None:
            owner = np.clip(np.searchsorted(edges, positions, side="right") - 1, 0, count - 1)
            np.minimum.at(lo, owner, lows)
            np.maximum.at(hi, owner, highs)

        if self._starts:
            starts = np.concatenate(self._starts)
            lengths = np.concatenate(self._lengths).astype(np.int64)
            assign(starts + (lengths - 1) // 2, np.concatenate(self._mins), np.concatenate(self._maxs))
        for start, data in zip(self._raw_starts, self._raw_data, strict=True):
            # Raw samples: each one is its own position, so the edges split them exactly.
            positions = np.arange(start, start + data.size, dtype=np.int64)
            owner = np.clip(np.searchsorted(edges, positions, side="right") - 1, 0, count - 1)
            first, last = int(owner[0]), int(owner[-1])
            if first == last:
                lo[first] = min(lo[first], data.min())
                hi[first] = max(hi[first], data.max())
            else:
                assign(positions, data, data)
        empty = lo > hi  # only reachable when the expected count was far off
        lo[empty] = 0
        hi[empty] = 0
        scale = np.float32(32768.0)
        return WaveformPeaks(
            buckets=[
                (float(np.float32(a) / scale), float(np.float32(b) / scale))
                for a, b in zip(lo.tolist(), hi.tolist(), strict=True)
            ]
        )


@dataclass(frozen=True)
class Loudness:
    """Loudness curve: RMS level of each consecutive `window_seconds` of the audio, in dBFS (0 is a
    full-scale square wave, quieter is negative, digital silence is floored at `LOUDNESS_FLOOR_DB`).
    `db[i]` covers `[i * window_seconds, (i + 1) * window_seconds)`; the last window may be shorter.
    Computed on the mono downmix at the waveform's sample rate."""

    window_seconds: float
    db: list[float]


class LoudnessAccumulator:
    def __init__(self, sample_rate: int, window_seconds: float = LOUDNESS_WINDOW_SECONDS) -> None:
        self._window_seconds = window_seconds
        self._window = max(1, round(sample_rate * window_seconds))
        self._db: list[float] = []
        self._acc = 0.0
        self._filled = 0

    def feed(self, samples: np.ndarray) -> None:
        x = samples.astype(np.float64)
        pos = 0
        while pos < x.size:
            take = min(self._window - self._filled, x.size - pos)
            part = x[pos : pos + take]
            self._acc += float(np.dot(part, part))
            self._filled += take
            pos += take
            if self._filled == self._window:
                self._close()

    def _close(self) -> None:
        rms = (self._acc / self._filled) ** 0.5 / 32768.0
        db = 20 * math.log10(rms) if rms > 0 else LOUDNESS_FLOOR_DB
        self._db.append(round(max(LOUDNESS_FLOOR_DB, db), 2))
        self._acc = 0.0
        self._filled = 0

    def finish(self) -> Loudness:
        if self._filled:
            self._close()
        return Loudness(window_seconds=self._window_seconds, db=list(self._db))


def stream_waveform(
    chunks: Iterable[np.ndarray],
    sample_rate: int,
    expected_samples: int | None = None,
    bucket_count: int = DEFAULT_BUCKET_COUNT,
) -> tuple[WaveformPeaks, Loudness]:
    """Peaks and loudness from one pass over mono int16 chunks (`ffmpeg.iter_pcm_chunks`)."""
    peaks = PeakAccumulator(expected_samples, bucket_count)
    loudness = LoudnessAccumulator(sample_rate)
    for chunk in chunks:
        peaks.feed(chunk)
        loudness.feed(chunk)
    return peaks.finish(), loudness.finish()
