import struct

from split_video.waveform import WaveformPeaks, compute_peaks


def _pcm(samples):
    return struct.pack(f"<{len(samples)}h", *samples)


def test_empty_input_produces_no_buckets():
    assert compute_peaks(b"") == WaveformPeaks(buckets=[])


def test_bucket_count_is_clamped_to_available_samples():
    peaks = compute_peaks(_pcm([0, 0, 0]), bucket_count=2000)
    assert len(peaks.buckets) == 3


def test_each_bucket_reports_normalized_min_and_max():
    # Two buckets of two samples each: [max positive, zero] and [min negative, zero]
    peaks = compute_peaks(_pcm([32767, 0, -32768, 0]), bucket_count=2)
    assert len(peaks.buckets) == 2
    (min0, max0), (min1, max1) = peaks.buckets
    assert min0 == 0.0
    assert max0 == 32767 / 32768.0
    assert min1 == -1.0
    assert max1 == 0.0


def test_buckets_span_the_full_sample_array_contiguously():
    samples = list(range(-10, 10))  # 20 samples
    peaks = compute_peaks(_pcm(samples), bucket_count=4)
    assert len(peaks.buckets) == 4
    # first bucket covers the most-negative samples, last the least-negative
    assert peaks.buckets[0][0] == -10 / 32768.0
    assert peaks.buckets[-1][1] == 9 / 32768.0
