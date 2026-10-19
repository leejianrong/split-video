"""Streaming peaks, loudness, silence parsing and classification must give what the whole-file
functions give (SPL-41). The real-ffmpeg and real-model parity checks are in
tests/integration/test_streaming_analysis_real.py."""

import math

import numpy as np
import pytest

from split_video.editor import classify
from split_video.editor.classify import SILENCE_BUCKET, ClassLabels
from split_video.silence import SilenceParser, parse_silencedetect_output
from split_video.waveform import (
    LOUDNESS_FLOOR_DB,
    LoudnessAccumulator,
    PeakAccumulator,
    compute_peaks,
    stream_waveform,
)


def _samples(n, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    wave = 12000 * np.sin(t / 37.0) * np.sin(t / 91000.0) + rng.normal(0, 3000, n)
    return np.clip(wave, -32768, 32767).astype("<i2")


def _chunks(samples, size):
    for i in range(0, samples.size, size):
        yield samples[i : i + size]


def _streamed(samples, expected, chunk, buckets=2000):
    acc = PeakAccumulator(expected, buckets)
    for c in _chunks(samples, chunk):
        acc.feed(c)
    return acc.finish()


@pytest.mark.parametrize("n", [1, 7, 1999, 2000, 2001, 5000, 80_000, 480_000, 1_300_017])
@pytest.mark.parametrize("chunk", [1, 4097, 40_000])
def test_peaks_are_identical_to_compute_peaks_when_the_expected_count_is_right(n, chunk):
    if chunk == 1 and n > 5000:
        pytest.skip("one-sample chunks are only for small inputs")
    samples = _samples(n)
    assert _streamed(samples, n, chunk) == compute_peaks(samples.tobytes())


@pytest.mark.parametrize("delta", [-400, -37, -1, 1, 5, 300, 510])
def test_peaks_stay_identical_when_the_expected_count_is_a_little_off(delta):
    # ffprobe's duration is only approximately the decoded sample count; the raw margin around each
    # expected bucket edge absorbs that.
    n = 900_000
    samples = _samples(n, seed=1)
    assert _streamed(samples, n + delta, 20_000) == compute_peaks(samples.tobytes())


@pytest.mark.parametrize("expected", [None, 0])
def test_peaks_without_an_expected_count_are_identical_too(expected):
    samples = _samples(123_456, seed=2)
    assert _streamed(samples, expected, 10_000) == compute_peaks(samples.tobytes())


def test_peaks_with_a_badly_wrong_expected_count_are_within_one_block_of_the_edges():
    n = 600_000
    samples = _samples(n, seed=3)
    old = compute_peaks(samples.tobytes()).buckets
    new = _streamed(samples, int(n * 1.2), 8000).buckets  # container duration 20% too long
    assert len(new) == len(old)
    # Not identical (edges snap to 64-sample blocks), but the envelope is the same shape: the mean
    # absolute difference per bucket edge is small. (Looser than needed; random noise has big extremes.)
    assert sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(old, new, strict=True)) / 2000 < 0.08


def test_empty_stream_has_no_buckets():
    assert PeakAccumulator(1000).finish().buckets == []
    assert PeakAccumulator(None).finish().buckets == []


def test_long_input_keeps_a_bounded_number_of_pieces():
    # pieces = raw margins around the edges plus one block per 64 samples elsewhere, never one per sample
    # (a 10 min file is mostly margin at 2000 buckets; the 3 h memory bound is in the integration test).
    n = 8000 * 600
    acc = PeakAccumulator(n)
    for c in _chunks(_samples(n, seed=4), 40_000):
        acc.feed(c)
    pieces = sum(a.size for a in acc._starts) + sum(d.size for d in acc._raw_data)
    assert pieces < 2001 * 2 * 512 + n // 256 + 4000
    assert pieces < n


def test_loudness_windows_and_units():
    rate = 8000
    loud = np.full(rate, 16384, dtype="<i2")  # constant half scale: RMS = 0.5 -> -6.02 dBFS
    quiet = np.zeros(rate, dtype="<i2")
    tail = np.full(rate // 2, 3277, dtype="<i2")
    acc = LoudnessAccumulator(rate)
    audio = np.concatenate([loud, quiet, tail])
    for c in _chunks(audio, 3333):  # chunk edges never line up with windows
        acc.feed(c)
    result = acc.finish()
    assert result.window_seconds == 1.0
    assert result.db == [-6.02, LOUDNESS_FLOOR_DB, round(20 * math.log10(3277 / 32768), 2)]


def test_loudness_matches_a_direct_computation():
    rate = 8000
    audio = _samples(rate * 10 + 123, seed=5)
    expected = []
    for i in range(0, audio.size, rate):
        w = audio[i : i + rate].astype(np.float64)
        expected.append(round(20 * math.log10(math.sqrt(np.mean(w * w)) / 32768), 2))
    acc = LoudnessAccumulator(rate)
    for c in _chunks(audio, 5000):
        acc.feed(c)
    assert acc.finish().db == expected


def test_stream_waveform_runs_both_in_one_pass():
    audio = _samples(8000 * 5, seed=6)
    peaks, loudness = stream_waveform(_chunks(audio, 7000), 8000, audio.size)
    assert peaks == compute_peaks(audio.tobytes()) and len(loudness.db) == 5


def test_silence_parser_matches_the_whole_text_parser_line_by_line():
    log = (
        "size=N/A time=00:00:01.00 bitrate=N/A\n"
        "[silencedetect @ 0x1] silence_start: 1.5\n"
        "[silencedetect @ 0x1] silence_end: 3 | silence_duration: 1.5\n"
        "[silencedetect @ 0x1] silence_start: 8\n"
    )
    parser = SilenceParser()
    for line in log.splitlines(keepends=True):
        parser.feed(line)
    assert parser.pending_start == 8.0
    assert [(i.start, i.end) for i in parser.intervals] == [(1.5, 3.0)]
    assert parse_silencedetect_output(log, 10.0)[-1].end == 10.0


# --- classification -----------------------------------------------------------------------------

LABELS = ClassLabels(
    display_names=["Music", "Speech", "Applause", "Noise"],
    bucket_indices={"music": [0], "speech": [1], "applause_crowd": [2], SILENCE_BUCKET: [3]},
)
THRESHOLDS = {"music": 0.3, "speech": 0.3, "applause_crowd": 0.3, SILENCE_BUCKET: 0.3}


class FakeModel:
    """Scores are a deterministic function of the whole frame, so any frame that sees different samples
    (offset, padding) scores differently."""

    def __init__(self):
        self.frames = 0

    def classify_frame(self, frame):
        assert frame.shape == (classify.FRAME_SAMPLES,) and frame.dtype == np.float32
        self.frames += 1
        energy = float(np.mean(frame * frame)) ** 0.5
        first = float(frame[0])
        mid = float(abs(frame[classify.FRAME_SAMPLES // 2]))
        return np.array(
            [min(1.0, energy * 4), min(1.0, abs(first) * 3), min(1.0, mid * 3), 0.2 + 0.5 * (energy < 0.02)]
        )


def _music_speech_silence(seconds):
    n = int(seconds * classify.SAMPLE_RATE)
    t = np.arange(n) / classify.SAMPLE_RATE
    third = n // 3
    wave = np.zeros(n)
    wave[:third] = 9000 * np.sin(2 * np.pi * 440 * t[:third])
    wave[third : 2 * third] = np.random.default_rng(1).normal(0, 4000, third)
    return np.clip(wave, -32768, 32767).astype("<i2")


def _old(audio):
    return classify.classify_audio(audio.tobytes(), FakeModel(), LABELS, THRESHOLDS)


@pytest.mark.parametrize("seconds", [0.0, 0.01, 0.5, 0.975, 0.98, 1.0, 3.3, 20.07])
@pytest.mark.parametrize("chunk", [1000, 7680, 15600, 80_000, 10**6])
def test_classify_stream_matches_classify_audio(seconds, chunk):
    audio = _music_speech_silence(seconds) if seconds else np.zeros(0, dtype="<i2")
    if chunk == 1000 and seconds > 4:
        pytest.skip("tiny chunks only for short audio")
    regions, scores, duration = classify.classify_stream(_chunks(audio, chunk), FakeModel(), LABELS, THRESHOLDS)
    old_regions, old_scores, old_duration = _old(audio)
    assert scores == old_scores
    assert regions == old_regions
    assert duration == old_duration


def test_classify_stream_progress_follows_decoded_position_and_ends_exact():
    audio = _music_speech_silence(30)
    calls = []
    classify.classify_stream(
        _chunks(audio, 16000),
        FakeModel(),
        LABELS,
        THRESHOLDS,
        lambda done, total: calls.append((done, total)),
        expected_samples=audio.size + 100,  # slightly off, as a probed duration is
    )
    dones = [d for d, _ in calls]
    assert dones == sorted(dones) and dones[0] == 1
    assert calls[-1][0] == calls[-1][1]  # corrected to the real count at the end
    assert all(total >= done for done, total in calls)


def test_classify_stream_consumes_chunks_lazily():
    pulled = []

    def chunks():
        for i in range(10):
            pulled.append(i)
            yield _music_speech_silence(2)

    model, seen = FakeModel(), []
    classify.classify_stream(chunks(), model, LABELS, THRESHOLDS, lambda d, t: seen.append(len(pulled)))
    assert seen[0] < 10  # the first frame was scored before the whole input had been read
