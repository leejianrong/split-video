"""Streaming analysis against real ffmpeg (and the real YAMNet model where present): parity with the
whole-file functions on short files, prompt cancel, progress, and a fixed memory bound on a long file
(SPL-41).

Set SPLIT_VIDEO_LONG_TESTS=1 to also run the 3-hour classification check (about a minute)."""

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from split_video import ffmpeg as ffmpeg_module
from split_video.editor import classify
from split_video.editor.app import create_app
from split_video.editor.classify import MODEL_PATH
from split_video.editor.schemas import StateParams
from split_video.ffmpeg import (
    ExtractError,
    detect_silence,
    extract_pcm_audio,
    iter_pcm_chunks,
    probe_duration,
)
from split_video.jobs import InProcessJobQueue, JobCancelledError
from split_video.silence import parse_silencedetect_output
from split_video.waveform import compute_peaks, stream_waveform

needs_model = pytest.mark.skipif(not MODEL_PATH.exists(), reason="YAMNet model not fetched (make fetch-model)")
DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)


class Token:
    """A minimal CancelToken."""

    def __init__(self):
        self._cancelled = threading.Event()
        self._callbacks = []

    def cancel(self):
        self._cancelled.set()
        for cb in list(self._callbacks):
            cb()

    def is_cancelled(self):
        return self._cancelled.is_set()

    def raise_if_cancelled(self):
        if self._cancelled.is_set():
            raise JobCancelledError()

    def on_cancel(self, callback):
        self._callbacks.append(callback)
        if self._cancelled.is_set():
            callback()
        return lambda: self._callbacks.remove(callback) if callback in self._callbacks else None


def _lavfi(path, graph, seconds, *extra):
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", graph, "-t", str(seconds), *extra]
    result = subprocess.run([*cmd, str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return path


def _mixed_clip(path, seconds, rate=16000, codec=("-c:a", "aac", "-b:a", "24k")):
    """Tone, noise and silence in rotation (50 s on, 10 s of digital silence, per minute) so silence,
    peaks, loudness and the classifier all have something to see."""
    graph = f"aevalsrc='if(lt(mod(t,60),20),0.5*sin(2*PI*440*t),if(lt(mod(t,60),50),0.3*(random(0)*2-1),0))':s={rate}"
    return _lavfi(path, graph, seconds, "-ac", "1", *codec)


@pytest.fixture(scope="module")
def short_clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("short")
    return [
        _mixed_clip(d / "mixed70.m4a", 70),
        _mixed_clip(d / "mixed23.m4a", 23.37),
        _mixed_clip(d / "mixed5.mp3", 5.1, codec=("-c:a", "libmp3lame", "-b:a", "48k")),
        _mixed_clip(d / "mixed2.wav", 2.0, codec=("-c:a", "pcm_s16le")),
        _lavfi(d / "tiny.wav", "sine=f=330:d=0.31", 0.31, "-ac", "1"),
    ]


@pytest.fixture(scope="module")
def one_hour(tmp_path_factory):
    return _mixed_clip(tmp_path_factory.mktemp("hour") / "hour.m4a", 3600)


# --- the reader ---------------------------------------------------------------------------------


@pytest.mark.parametrize("rate", [8000, 16000])
def test_chunks_concatenate_to_exactly_what_extract_pcm_audio_returns(short_clips, rate):
    for clip in short_clips:
        old = np.frombuffer(extract_pcm_audio(clip, sample_rate=rate), dtype="<i2")
        chunks = list(iter_pcm_chunks(clip, rate, chunk_seconds=1.5))
        assert all(c.dtype == np.dtype("<i2") and 0 < c.size <= int(rate * 1.5) for c in chunks)
        assert all(c.size == int(rate * 1.5) for c in chunks[:-1])
        assert np.array_equal(np.concatenate(chunks), old), clip.name


def test_a_file_that_is_not_media_fails_with_the_same_message(tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"this is not a video")
    with pytest.raises(ExtractError) as old:
        extract_pcm_audio(bad)
    with pytest.raises(ExtractError) as new:
        list(iter_pcm_chunks(bad))
    assert str(new.value).startswith("ffmpeg failed to decode audio from 'broken.mp4': ")
    # same ffmpeg complaint, whatever the banner/stdin options around it
    assert str(old.value).split(": ", 1)[1].splitlines()[-1] in str(new.value)


def test_a_video_without_audio_fails_cleanly(tmp_path):
    silent = _lavfi(tmp_path / "noaudio.mp4", "color=c=black:s=64x64:r=5", 1, "-c:v", "libx264")
    with pytest.raises(ExtractError, match="noaudio.mp4"):
        list(iter_pcm_chunks(silent))


def test_closing_the_generator_early_stops_ffmpeg(one_hour, monkeypatch):
    clip = one_hour
    procs = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        ffmpeg_module.subprocess, "Popen", lambda *a, **k: procs.append(real_popen(*a, **k)) or procs[-1]
    )
    gen = iter_pcm_chunks(clip, chunk_seconds=1)
    next(gen)
    gen.close()
    assert procs[0].wait(5) is not None


def test_cancel_mid_file_terminates_ffmpeg_promptly(one_hour, monkeypatch):
    clip = one_hour
    procs = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        ffmpeg_module.subprocess, "Popen", lambda *a, **k: procs.append(real_popen(*a, **k)) or procs[-1]
    )
    token = Token()
    gen = iter_pcm_chunks(clip, chunk_seconds=1, cancel=token)
    next(gen)
    token.cancel()
    started = time.monotonic()
    with pytest.raises(JobCancelledError):
        for _ in gen:
            pass
    assert time.monotonic() - started < 2.0
    assert procs[0].wait(2) is not None  # ffmpeg is gone, not left decoding the rest of the hour


def test_a_slow_consumer_never_stalls_ffmpeg_on_a_full_pipe(one_hour):
    clip = one_hour
    total = 0
    for i, chunk in enumerate(iter_pcm_chunks(clip, chunk_seconds=1)):
        total += chunk.size
        if i == 3:
            time.sleep(1.0)  # ffmpeg fills the pipe meanwhile and must simply wait
    assert abs(total - 3600 * 8000) < 8000


# --- parity on real files ------------------------------------------------------------------------


def test_peaks_match_the_old_pipeline_on_short_files(short_clips):
    for clip in short_clips:
        old = compute_peaks(extract_pcm_audio(clip))
        expected = int(probe_duration(clip) * ffmpeg_module.WAVEFORM_SAMPLE_RATE)
        peaks, loudness = stream_waveform(iter_pcm_chunks(clip), ffmpeg_module.WAVEFORM_SAMPLE_RATE, expected)
        assert peaks == old, clip.name
        assert abs(len(loudness.db) - probe_duration(clip)) <= 1


def test_silence_matches_the_old_whole_stderr_parse(short_clips, three_songs_clip, leading_trailing_silence_clip):
    for clip in [*short_clips, three_songs_clip, leading_trailing_silence_clip]:
        for threshold, min_dur in [("-35dB", 0.05), ("-50dB", 1.0)]:
            result = subprocess.run(
                ["ffmpeg", "-i", str(clip), "-af", f"silencedetect=noise={threshold}:d={min_dur}", "-f", "null", "-"],
                capture_output=True,
                text=True,
                check=False,
            )
            old = parse_silencedetect_output(result.stderr, probe_duration(clip))
            assert detect_silence(clip, threshold, min_dur) == old, (clip.name, threshold)


def test_a_file_that_ends_in_silence_closes_the_last_interval_at_the_duration(tmp_path):
    clip = _lavfi(tmp_path / "tail.wav", "aevalsrc='if(lt(t,2),0.5*sin(2*PI*440*t),0)':s=16000", 5, "-ac", "1")
    found = detect_silence(clip, "-40dB", 0.5)
    assert len(found) == 1 and found[0].end == pytest.approx(probe_duration(clip))


def test_silence_scan_can_be_cancelled(one_hour):
    clip = one_hour
    token = Token()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(JobCancelledError):
        detect_silence(clip, "-35dB", 0.05, cancel=token)
    assert time.monotonic() - started < 3.0


@needs_model
def test_classification_matches_classify_audio_on_short_files_with_the_real_model(short_clips):
    model, labels = classify.get_model_and_labels()
    for clip in short_clips:
        pcm = extract_pcm_audio(clip, sample_rate=classify.SAMPLE_RATE)
        old_regions, old_scores, old_duration = classify.classify_audio(pcm, model, labels, classify.DEFAULT_THRESHOLDS)
        expected = int(probe_duration(clip) * classify.SAMPLE_RATE)
        regions, scores, duration = classify.classify_stream(
            iter_pcm_chunks(clip, classify.SAMPLE_RATE, chunk_seconds=3.3),
            model,
            labels,
            classify.DEFAULT_THRESHOLDS,
            expected_samples=expected,
        )
        assert duration == old_duration
        assert scores == old_scores, clip.name
        assert regions == old_regions, clip.name


# --- job, progress, cancel through the real pipeline ---------------------------------------------


class _ScoringModel:
    """Stands in for YAMNet (so these tests need no model file) but sees the real decoded audio."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.frames = 0

    def classify_frame(self, frame):
        self.frames += 1
        time.sleep(self.delay)
        return np.array([float(abs(frame).mean()) * 4, 0.1, 0.1, 0.1, 0.1, 0.4])


_LABELS = classify.ClassLabels(
    display_names=list(classify.BUCKET_LABELS),
    bucket_indices={bucket: [i] for i, bucket in enumerate(classify.BUCKET_LABELS)},
)


def test_analysis_job_reports_progress_up_to_the_real_frame_count(three_songs_clip, monkeypatch):
    monkeypatch.setattr(classify, "get_model_and_labels", lambda: (_ScoringModel(), _LABELS))
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS, jobs=InProcessJobQueue(synchronous=True)))
    job_id = client.post("/api/v1/analyze", params={"path": three_songs_clip.name}).json()["job_id"]
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    # 21 s of audio: ceil(21 * 16000 / 7680) = 44 frames, and the bar ends exactly full.
    assert job["state"] == "done" and job["completed"] == job["total"] == 44
    result = client.get("/api/v1/classification", params={"path": three_songs_clip.name}).json()
    assert result["analyzed"] is True


def test_progress_tracks_the_decode_position_not_wall_time(tmp_path, monkeypatch):
    from split_video import project
    from split_video.editor.cache import ClassificationCache
    from split_video.local_media_store import LocalMediaStore

    clip = _mixed_clip(tmp_path / "three_min.m4a", 180)
    model = _ScoringModel()
    monkeypatch.setattr(classify, "get_model_and_labels", lambda: (model, _LABELS))
    media = LocalMediaStore(tmp_path)
    cache = ClassificationCache(media, clip.name)
    seen = []
    project.run_analysis(media, clip.name, cache, cache.get_thresholds(), lambda d, t: seen.append((d, t)))
    frames = seen[-1][0]
    assert frames in (375, 376)
    done = [d for d, _ in seen]
    assert sorted(set(done)) == list(range(1, frames + 1)) and done == sorted(done)  # every frame, in order
    assert all(abs(t - 375) <= 1 for _, t in seen)  # total estimated from the probed duration up front
    assert seen[-1] == (frames, frames)  # and corrected to the real count at the end


def test_cancelling_run_analysis_stops_ffmpeg_and_stores_nothing(one_hour, monkeypatch):
    from split_video import project
    from split_video.editor.cache import ClassificationCache
    from split_video.local_media_store import LocalMediaStore

    clip = one_hour
    tmp_path = clip.parent
    model = _ScoringModel(delay=0.002)
    monkeypatch.setattr(classify, "get_model_and_labels", lambda: (model, _LABELS))
    procs = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        ffmpeg_module.subprocess, "Popen", lambda *a, **k: procs.append(real_popen(*a, **k)) or procs[-1]
    )
    media = LocalMediaStore(tmp_path)
    cache = ClassificationCache(media, clip.name)
    token = Token()

    def on_progress(done, total):
        if done == 20:
            token.cancel()

    started = time.monotonic()
    with pytest.raises(JobCancelledError):
        project.run_analysis(media, clip.name, cache, cache.get_thresholds(), on_progress, cancel=token)
    assert time.monotonic() - started < 5.0
    assert model.frames < 40 and not cache.is_analyzed
    decoder = next(p for p in procs if p.args[0].endswith("ffmpeg"))
    assert decoder.wait(5) is not None  # terminated, not left decoding the rest of the hour


# --- memory bounds ------------------------------------------------------------------------------

# Child process: reports its own peak RSS (ru_maxrss) after the work, so the parent pytest process's
# memory is not part of it. Python + numpy alone is ~40 MB of that.
_CHILD_FILE = textwrap.dedent(
    """
    import resource, sys
    from pathlib import Path
    from split_video import ffmpeg
    from split_video.waveform import stream_waveform
    path = Path(sys.argv[1]); duration = ffmpeg.probe_duration(path)
    peaks, loud = stream_waveform(ffmpeg.iter_pcm_chunks(path), 8000, int(duration * 8000))
    silences = ffmpeg.detect_silence(path, "-35dB", 0.05)
    samples = sum(c.size for c in ffmpeg.iter_pcm_chunks(path, 16000))  # the classifier's decode
    print(len(peaks.buckets), len(loud.db), len(silences), samples)
    print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
    """
)

_CHILD_SAMPLES = textwrap.dedent(
    """
    import resource, sys
    import numpy as np
    from split_video.waveform import stream_waveform
    hours = float(sys.argv[1]); rate = 8000; n = int(hours * 3600 * rate); chunk = 5 * rate
    block = np.random.default_rng(0).integers(-20000, 20000, chunk).astype("<i2")
    def chunks():
        for i in range(0, n, chunk):
            yield block[: min(chunk, n - i)]
    peaks, loud = stream_waveform(chunks(), rate, n)
    print(len(peaks.buckets), len(loud.db))
    print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
    """
)

_CHILD_CLASSIFY = textwrap.dedent(
    """
    import resource, sys
    from pathlib import Path
    from split_video import project
    from split_video.editor.cache import ClassificationCache
    from split_video.local_media_store import LocalMediaStore
    path = Path(sys.argv[1]); media = LocalMediaStore(path.parent)
    cache = ClassificationCache(media, path.name)
    project.run_analysis(media, path.name, cache, cache.get_thresholds())
    print(len(cache.get_regions()))
    print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
    """
)


def _child(code, *args, timeout=600):
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    *out, rss = result.stdout.split()
    return out, int(rss)


# One hour is the largest file the old code can be run on this machine's CI comfortably; it peaks at
# about 730 MB on it (whole decode as bytes, then as float32). The streaming path stays far below.
PEAK_RSS_BOUND_MB = 250


def test_peaks_loudness_silence_and_decode_of_a_one_hour_file_fit_in_a_fixed_memory_bound(one_hour):
    out, rss = _child(_CHILD_FILE, str(one_hour))
    peaks, loudness, silences, samples = map(int, out)
    assert peaks == 2000 and loudness == 3600 and silences >= 60 and abs(samples - 3600 * 16000) < 16000
    assert rss < PEAK_RSS_BOUND_MB, f"peak RSS {rss} MB"


def test_three_hours_of_samples_through_peaks_and_loudness_fit_in_a_fixed_memory_bound():
    # The sample count of a 3 h file at the waveform rate (86.4 M), fed in 5 s chunks without a decode.
    out, rss = _child(_CHILD_SAMPLES, "3")
    assert [int(x) for x in out] == [2000, 10800]
    assert rss < PEAK_RSS_BOUND_MB, f"peak RSS {rss} MB"


@needs_model
def test_classifying_a_one_hour_file_with_the_real_model_fits_in_a_fixed_memory_bound(one_hour):
    started = time.monotonic()
    out, rss = _child(_CHILD_CLASSIFY, str(one_hour))
    print(f"classified 1 h in {time.monotonic() - started:.1f}s, peak RSS {rss} MB")
    assert int(out[0]) >= 1
    assert rss < PEAK_RSS_BOUND_MB, f"peak RSS {rss} MB"


@needs_model
@pytest.mark.skipif(not os.environ.get("SPLIT_VIDEO_LONG_TESTS"), reason="set SPLIT_VIDEO_LONG_TESTS=1 (about 1 min)")
def test_everything_over_a_three_hour_file_fits_in_a_fixed_memory_bound(tmp_path):
    clip = _mixed_clip(tmp_path / "three_hours.m4a", 3 * 3600)
    out, rss = _child(_CHILD_FILE, str(clip))
    assert rss < PEAK_RSS_BOUND_MB, f"peak RSS {rss} MB (peaks/loudness/silence)"
    out, rss = _child(_CHILD_CLASSIFY, str(clip))
    assert rss < PEAK_RSS_BOUND_MB, f"peak RSS {rss} MB (classification)"
    assert Path(clip).exists()
