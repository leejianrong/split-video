"""Round-trip tests for editor/cache.py's sidecar persistence.

Each test constructs a cache, lets it "compute" (via a monkeypatched
ffmpeg-touching function, so no real ffmpeg process is involved) and save,
then constructs a *second* instance pointed at the same source — standing
in for a later `split-video edit` process — and checks it loads from disk
instead of recomputing.
"""

import struct

from split_video.editor import cache as cache_module
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.editor.classify import DEFAULT_THRESHOLDS
from split_video.silence import SilenceInterval


def _touch(path, content=b"hello"):
    path.write_bytes(content)
    return path


def _pcm(samples):
    return struct.pack(f"<{len(samples)}h", *samples)


def _fail_if_called(*args, **kwargs):
    raise AssertionError("should not recompute once the sidecar cache is warm")


def test_silence_cache_reloads_from_disk_without_recomputing(tmp_path, monkeypatch):
    source = _touch(tmp_path / "video.mp4")
    monkeypatch.setattr(cache_module, "detect_silence", lambda *a, **k: [SilenceInterval(start=1.0, end=2.0)])

    first = SilenceCache(source)
    assert first.get_raw_silences(-35.0) == [SilenceInterval(start=1.0, end=2.0)]

    monkeypatch.setattr(cache_module, "detect_silence", _fail_if_called)
    second = SilenceCache(source)
    assert second.get_raw_silences(-35.0) == [SilenceInterval(start=1.0, end=2.0)]


def test_silence_cache_keeps_multiple_thresholds_on_disk(tmp_path, monkeypatch):
    source = _touch(tmp_path / "video.mp4")
    calls = []

    def fake_detect_silence(source_arg, threshold_db, min_duration):
        calls.append(threshold_db)
        return [SilenceInterval(start=float(len(calls)), end=float(len(calls) + 1))]

    monkeypatch.setattr(cache_module, "detect_silence", fake_detect_silence)
    first = SilenceCache(source)
    first.get_raw_silences(-35.0)
    first.get_raw_silences(-40.0)

    monkeypatch.setattr(cache_module, "detect_silence", _fail_if_called)
    second = SilenceCache(source)
    assert second.get_raw_silences(-35.0) == [SilenceInterval(start=1.0, end=2.0)]
    assert second.get_raw_silences(-40.0) == [SilenceInterval(start=2.0, end=3.0)]


def test_waveform_cache_reloads_from_disk_without_recomputing(tmp_path, monkeypatch):
    source = _touch(tmp_path / "video.mp4")
    monkeypatch.setattr(cache_module, "extract_pcm_audio", lambda *a, **k: _pcm([32767, 0, -32768, 0]))

    first = WaveformCache(source)
    peaks = first.get_peaks()
    assert len(peaks.buckets) > 0

    monkeypatch.setattr(cache_module, "extract_pcm_audio", _fail_if_called)
    second = WaveformCache(source)
    assert second.get_peaks() == peaks


def test_classification_cache_reloads_frame_scores_and_rederives_regions(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    frame_bucket_scores = [
        {"music": 0.9, "singing": 0.1, "speech": 0.05, "applause_crowd": 0.02, "laughter": 0.01, "silence_other": 0.02},
        {
            "music": 0.85,
            "singing": 0.1,
            "speech": 0.05,
            "applause_crowd": 0.02,
            "laughter": 0.01,
            "silence_other": 0.02,
        },
    ]
    total_duration = 0.96
    expected_regions = classify.rethreshold(frame_bucket_scores, DEFAULT_THRESHOLDS, total_duration)

    first = ClassificationCache(source)
    first.store_result(frame_bucket_scores, total_duration, regions=expected_regions)

    second = ClassificationCache(source)
    assert second.is_analyzed
    assert second.get_regions() == expected_regions
    # A fresh session still starts at default thresholds, exactly as an
    # in-memory-only cache would — thresholds themselves aren't persisted.
    assert second.get_thresholds() == dict(DEFAULT_THRESHOLDS)


def test_classification_cache_without_prior_analysis_is_not_analyzed(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    cache = ClassificationCache(source)
    assert not cache.is_analyzed
    assert cache.get_regions() == []


def test_cache_ignores_stale_sidecar_after_source_changes(tmp_path, monkeypatch):
    source = _touch(tmp_path / "video.mp4")
    monkeypatch.setattr(cache_module, "detect_silence", lambda *a, **k: [SilenceInterval(start=1.0, end=2.0)])
    first = SilenceCache(source)
    first.get_raw_silences(-35.0)

    _touch(source, content=b"a completely different, longer recording")
    monkeypatch.setattr(cache_module, "detect_silence", lambda *a, **k: [SilenceInterval(start=9.0, end=9.5)])
    second = SilenceCache(source)
    assert second.get_raw_silences(-35.0) == [SilenceInterval(start=9.0, end=9.5)]
