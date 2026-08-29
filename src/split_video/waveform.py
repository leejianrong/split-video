"""Peak min/max bucketing of raw PCM audio samples for the timeline's waveform overlay."""

from __future__ import annotations

import itertools
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
