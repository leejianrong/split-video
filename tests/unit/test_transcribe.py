"""Transcription engine against a fake model (no faster-whisper needed): windows, stitching, progress, cancel,
the stored format, currency, and the missing-dependency hint."""

import itertools
import os
import sys
import threading
import time

import numpy as np
import pytest
from fake_whisper import SAMPLE_RATE, FakeModel, speech, write_wav

from split_video import assets, job_work, transcribe
from split_video.editor.job_routes import cancel_job
from split_video.ffmpeg import MediaInfo
from split_video.jobs import InProcessJobQueue, JobCancelledError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import InMemoryMediaStore


class Cancel:
    """A CancelToken a test can fire."""

    def __init__(self) -> None:
        self.flag = False

    def is_cancelled(self) -> bool:
        return self.flag

    def raise_if_cancelled(self) -> None:
        if self.flag:
            raise JobCancelledError("cancelled")

    def on_cancel(self, callback):
        return lambda: None


def chunks(pcm, seconds=2.0):
    step = int(seconds * SAMPLE_RATE)
    return [pcm[i : i + step] for i in range(0, len(pcm), step)]


# ~1 word per 1.1 s over 61 s: bursts of 0.4 s with 0.7 s of digital silence between them.
STARTS = [0.5 + 1.1 * k for k in range(55)]


def test_windows_do_not_overlap_cover_everything_and_end_in_silence():
    pcm, truth = speech(STARTS, 62.0)
    got = list(transcribe.windows(chunks(pcm), window_seconds=10, search_seconds=4))
    assert len(got) > 4
    assert np.array_equal(np.concatenate([d for _, d in got]), pcm)  # nothing lost, nothing twice
    assert [s for s, _ in got] == np.cumsum([0] + [len(d) for _, d in got])[:-1].tolist()
    for start, data in got[:-1]:
        assert 0.5 * 10 * SAMPLE_RATE < len(data) <= 10 * SAMPLE_RATE + 2 * SAMPLE_RATE
        end = (start + len(data)) / SAMPLE_RATE
        assert not any(s < end < e for _, s, e in truth), "a window ended inside a word"


def test_short_audio_is_one_window_and_empty_audio_none():
    pcm, _ = speech([0.5], 3.0)
    assert [(s, len(d)) for s, d in transcribe.windows(chunks(pcm))] == [(0, len(pcm))]
    assert list(transcribe.windows([])) == []


def test_stitched_windows_give_every_word_once_at_its_absolute_time():
    pcm, truth = speech(STARTS, 62.0)
    model = FakeModel()
    done = transcribe.run(chunks(pcm), model, total_seconds=62.0, window_seconds=10, search_seconds=4)
    assert len(model.calls) > 4  # it really was cut into windows
    words = [w for s in done["segments"] for w in s["words"]]
    assert [w["word"] for w in words] == [t[0] for t in truth]  # in order, none repeated or missing
    for w, (_, start, end) in zip(words, truth, strict=True):
        assert w["start"] == pytest.approx(start, abs=0.002) and w["end"] == pytest.approx(end, abs=0.002)
    assert done["duration"] == pytest.approx(62.0) and done["language"] == "en"
    for s in done["segments"]:
        assert s["start"] == s["words"][0]["start"] and s["end"] == s["words"][-1]["end"]
        assert s["text"] == " ".join(w["word"] for w in s["words"])


def test_the_model_is_called_the_way_the_design_says():
    pcm, _ = speech([0.5, 2.0], 4.0)
    model = FakeModel()
    transcribe.run(chunks(pcm), model, language="fr")
    (call,) = model.calls
    assert call["dtype"] == "float32" and call["word_timestamps"] is True and call["vad_filter"] is True
    assert call["condition_on_previous_text"] is False and call["language"] == "fr"


def test_a_detected_language_is_passed_to_the_later_windows():
    pcm, _ = speech(STARTS[:25], 30.0)
    model = FakeModel(language="de")
    done = transcribe.run(chunks(pcm), model, window_seconds=10, search_seconds=4)
    assert [c["language"] for c in model.calls] == [None] + ["de"] * (len(model.calls) - 1)
    assert done["language"] == "de"


def test_progress_is_monotonic_and_reaches_the_end():
    pcm, _ = speech(STARTS, 62.0)
    seen = []
    transcribe.run(
        chunks(pcm),
        FakeModel(),
        total_seconds=62.0,
        window_seconds=10,
        search_seconds=4,
        on_progress=lambda d, t: seen.append((d, t)),
    )
    done = [d for d, _ in seen]
    assert done == sorted(done) and done[-1] == pytest.approx(62.0) and {t for _, t in seen} == {62.0}
    assert len(seen) > 20


def test_cancel_stops_between_segments():
    pcm, _ = speech(STARTS, 62.0)
    cancel = Cancel()
    model = FakeModel(per_segment=1, on_segment=lambda i: setattr(cancel, "flag", i >= 2))
    with pytest.raises(JobCancelledError):
        transcribe.run(chunks(pcm), model, window_seconds=10, search_seconds=4, cancel=cancel)
    assert len(model.calls) == 1  # it did not go on to the next window


def test_silence_gives_no_segments():
    done = transcribe.run(chunks(np.zeros(5 * SAMPLE_RATE, dtype="<i2")), FakeModel())
    assert done["segments"] == [] and done["duration"] == 5.0


def test_words_between_excerpt_and_text_between():
    doc = {
        "segments": [
            {
                "start": 1.0,
                "end": 3.0,
                "text": "a b c",
                "words": [
                    {"start": 1.0, "end": 1.5, "word": "a", "probability": 1.0},
                    {"start": 1.5, "end": 2.2, "word": "b", "probability": 1.0},
                    {"start": 2.5, "end": 3.0, "word": "c", "probability": 1.0},
                ],
            },
            {
                "start": 5.0,
                "end": 5.5,
                "text": "d",
                "words": [{"start": 5.0, "end": 5.5, "word": "d", "probability": 1.0}],
            },
        ]
    }
    assert [w["word"] for w in transcribe.words_between(doc, 0, 100)] == ["a", "b", "c", "d"]
    assert [w["word"] for w in transcribe.words_between(doc, 2.0, 2.6)] == ["b", "c"]  # a cut word is included
    assert transcribe.words_between(doc, 3.0, 5.0) == []  # [start, end): touching is not overlapping
    assert transcribe.text_between(doc, 1.6, 6) == "b c d"
    cut = transcribe.excerpt({"model": "m", **doc}, 2.0, 2.6)
    assert cut["model"] == "m" and [s["text"] for s in cut["segments"]] == ["b c"]
    assert (cut["segments"][0]["start"], cut["segments"][0]["end"]) == (1.5, 3.0)
    assert transcribe.excerpt(doc) is doc


# --- storing: the same behaviour on both media stores --------------------------------------------------


@pytest.fixture(params=["local", "memory"])
def store(request, tmp_path):
    pcm, truth = speech(STARTS[:12], 14.0)
    if request.param == "local":
        media = LocalMediaStore(tmp_path)
        write_wav(tmp_path / "talk.wav", pcm)
    else:
        media = InMemoryMediaStore(materialise=True)
        write_wav(tmp_path / "talk.wav", pcm)
        info = MediaInfo(duration=14.0, width=None, height=None, has_audio=True)
        media.add("talk.wav", (tmp_path / "talk.wav").read_bytes(), info=info)
    asset = assets.register_path(media, "talk.wav")
    return media, asset, truth


def test_transcribe_stores_the_documented_format(store):
    media, asset, truth = store
    model = FakeModel()
    seen = []
    result = transcribe.transcribe(
        media, asset.id, model_factory=lambda name, models: model, on_progress=lambda d, t: seen.append((d, t))
    )
    assert result == {
        "version": 1,
        "asset": asset.id,
        "model": "base.en",
        "language": "en",
        "duration": 14.0,
        "segments": 4,
        "words": 12,
        "made": True,
    }
    doc = transcribe.load(media, asset.id)
    assert list(doc) == ["version", "asset", "model", "language", "language_probability", "duration", "vad", "segments"]
    assert doc["version"] == 1 and doc["asset"] == asset.id and doc["vad"] is True
    first = doc["segments"][0]
    assert set(first) == {"start", "end", "text", "words"}
    assert set(first["words"][0]) == {"start", "end", "word", "probability"}
    words = [w for s in doc["segments"] for w in s["words"]]
    assert [w["word"] for w in words] == [t[0] for t in truth]  # stripped of the model's leading space
    assert all(a["end"] <= b["start"] + 1e-9 and a["start"] <= a["end"] for a, b in itertools.pairwise(words))
    assert seen[0] == (0.0, 14.0) and seen[-1][0] == pytest.approx(14.0)
    summary = assets.get_asset(media, asset.id)
    assert transcribe.has_transcript(media, summary) and transcribe.info(media, summary)["words"] == 12


def test_a_current_transcript_is_not_redone_unless_forced_or_different(store):
    media, asset, _ = store
    models = []

    def factory(name, directory):
        models.append((name, directory))
        return FakeModel()

    assert transcribe.transcribe(media, asset.id, model_factory=factory)["made"] is True
    assert transcribe.transcribe(media, asset.id, model_factory=factory)["made"] is False
    assert transcribe.transcribe(media, asset.id, language="en", model_factory=factory)["made"] is False
    assert len(models) == 1  # the model was not even loaded
    assert transcribe.transcribe(media, asset.id, force=True, model_factory=factory)["made"] is True
    assert transcribe.transcribe(media, asset.id, model="small.en", model_factory=factory)["model"] == "small.en"
    # A different model (the stored one is small.en now) or a different language is a new transcript.
    assert transcribe.transcribe(media, asset.id, model="small.en", model_factory=factory)["made"] is False
    assert transcribe.transcribe(media, asset.id, model="small.en", language="fr", model_factory=factory)["made"]
    assert [m for m, _ in models] == ["base.en", "base.en", "small.en", "small.en"]


def test_the_model_goes_in_the_stores_models_dir_not_the_home_cache(store):
    media, asset, _ = store
    got = []
    transcribe.transcribe(media, asset.id, model_factory=lambda name, d: got.append(d) or FakeModel())
    with media.models_dir() as expected:
        assert got == [str(expected)] and expected.is_dir()
    if isinstance(media, LocalMediaStore):
        assert got[0] == str(media.root / ".split-video-models")


def test_a_changed_file_makes_the_transcript_stale(store, tmp_path):
    media, asset, _ = store
    transcribe.transcribe(media, asset.id, model_factory=lambda n, d: FakeModel())
    assert transcribe.load(media, asset.id) is not None
    if isinstance(media, LocalMediaStore):
        os.utime(tmp_path / "talk.wav", (1_000_000_000, 1_000_000_000))  # same bytes, newer or older file
    else:
        media.add("talk.wav", media.read("talk.wav"), info=media.probe("talk.wav"), mtime=2.0)
    assert transcribe.load(media, asset.id) is None
    assert not transcribe.has_transcript(media, assets.get_asset(media, asset.id))


def test_a_cancelled_run_stores_nothing(store):
    media, asset, _ = store
    cancel = Cancel()
    model = FakeModel(per_segment=1, on_segment=lambda i: setattr(cancel, "flag", True))
    with pytest.raises(JobCancelledError):
        transcribe.transcribe(media, asset.id, model_factory=lambda n, d: model, cancel=cancel)
    assert transcribe.load(media, asset.id) is None


def test_an_asset_without_audio_is_refused(tmp_path):
    media = InMemoryMediaStore()
    media.add("mute.mp4", b"x", info=MediaInfo(duration=5.0, width=64, height=64, has_audio=False))
    asset = assets.register_path(media, "mute.mp4")
    with pytest.raises(transcribe.TranscriptionError, match="no audio"):
        transcribe.transcribe(media, asset.id, model_factory=lambda n, d: FakeModel())


def test_load_of_an_unknown_asset_raises():
    with pytest.raises(Exception, match="unknown asset"):
        transcribe.load(InMemoryMediaStore(), "a_0000000000000000")


# --- the optional dependency ----------------------------------------------------------------------------


@pytest.fixture
def no_faster_whisper(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # `import faster_whisper` raises ImportError


def test_missing_dependency_gives_the_install_hint(no_faster_whisper):
    assert transcribe.available() is False
    with pytest.raises(transcribe.TranscriptionUnavailableError) as raised:
        transcribe.require_available()
    text = str(raised.value)
    assert "uv sync --extra transcribe" in text and "pip install 'split-video[transcribe]'" in text
    assert "WITH_TRANSCRIPTION=1" in text and text.startswith(transcribe.UNAVAILABLE)


def test_missing_dependency_is_reported_before_the_audio_or_model_is_touched(no_faster_whisper, store):
    media, asset, _ = store
    with pytest.raises(transcribe.TranscriptionUnavailableError):
        transcribe.transcribe(media, asset.id)
    with pytest.raises(transcribe.TranscriptionUnavailableError):
        transcribe.load_model("base.en", "/nonexistent")
    with media.models_dir() as models:
        assert list(models.iterdir()) == []  # nothing was downloaded or created


def test_a_current_stored_transcript_is_returned_without_the_extra(store, monkeypatch):
    media, asset, _ = store
    assert transcribe.transcribe(media, asset.id, model_factory=lambda n, d: FakeModel())["made"] is True
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    again = transcribe.transcribe(media, asset.id)  # the default path: no model_factory, extra unavailable
    assert again["made"] is False and again["words"] == 12 and again["asset"] == asset.id
    # Anything that needs a model still raises: force, another model, another language.
    for kwargs in ({"force": True}, {"model": "small.en"}, {"language": "fr"}):
        with pytest.raises(transcribe.TranscriptionUnavailableError):
            transcribe.transcribe(media, asset.id, **kwargs)
    assert transcribe.info(media, asset)["model"] == "base.en"  # the stored one is untouched


def test_a_stale_transcript_without_the_extra_still_raises(store, tmp_path, monkeypatch):
    media, asset, _ = store
    transcribe.transcribe(media, asset.id, model_factory=lambda n, d: FakeModel())
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    other = transcribe.info(media, asset)
    media.save_cache(asset.path, transcribe.INFO_NAME, {**other, "asset": "a_0000000000000000"})
    with pytest.raises(transcribe.TranscriptionUnavailableError):
        transcribe.transcribe(media, asset.id)


def test_the_hugging_face_cache_is_pointed_into_the_workspace(monkeypatch, tmp_path):
    for var in ("HF_HOME", "HF_HUB_DISABLE_XET", "HF_HUB_DISABLE_TELEMETRY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(sys.modules, "faster_whisper", object())
    transcribe._import_faster_whisper(str(tmp_path))
    assert os.environ["HF_HOME"] == str(tmp_path) and os.environ["HF_HUB_DISABLE_XET"] == "1"


# --- loading the model: workspace first, then the image's seed, then the network -------------------------


class _FakeModule:
    """A `faster_whisper` that has models only in the folders listed in `have`."""

    def __init__(self, have):
        self.have = have
        self.calls = []

        def whisper_model(name, **kwargs):
            self.calls.append((kwargs["download_root"], kwargs["local_files_only"]))
            if kwargs["download_root"] in self.have or not kwargs["local_files_only"]:
                return f"model@{kwargs['download_root']}"
            raise OSError("not there")

        self.WhisperModel = whisper_model


def test_load_model_prefers_the_workspace_then_the_seed_then_downloads(monkeypatch):
    monkeypatch.setenv(transcribe.SEED_ENV, "/seed")
    fake = _FakeModule(have={"/ws/models"})
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    assert transcribe.load_model("base.en", "/ws/models") == "model@/ws/models"
    assert fake.calls == [("/ws/models", True)]

    fake = _FakeModule(have={"/seed"})
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    assert transcribe.load_model("base.en", "/ws/models") == "model@/seed"
    assert fake.calls == [("/ws/models", True), ("/seed", True)]  # never the network

    fake = _FakeModule(have=set())
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    assert transcribe.load_model("base.en", "/ws/models") == "model@/ws/models"  # downloaded into the workspace
    assert fake.calls == [("/ws/models", True), ("/seed", True), ("/ws/models", False)]


def test_load_model_failure_says_what_to_do(monkeypatch):
    monkeypatch.delenv(transcribe.SEED_ENV, raising=False)

    def broken(name, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setitem(sys.modules, "faster_whisper", type("M", (), {"WhisperModel": staticmethod(broken)}))
    with pytest.raises(transcribe.TranscriptionError, match="no route to host.*internet access once"):
        transcribe.load_model("base.en", "/ws/models")


# --- cancelling a model that cannot be interrupted (SPL-108) ------------------------------------------------


def _fire_after(cancel, seconds):
    threading.Timer(seconds, lambda: setattr(cancel, "flag", True)).start()


@pytest.mark.parametrize("model_kwargs", [{"segment_delay": 5.0}, {"call_delay": 5.0}])
def test_cancel_is_seen_while_the_model_is_busy(model_kwargs):
    """The model blocks for 5 s inside a segment (or inside `transcribe()` itself): the run still stops at once."""
    pcm, _ = speech(STARTS, 62.0)
    cancel = Cancel()
    _fire_after(cancel, 0.3)
    started = time.monotonic()
    with pytest.raises(JobCancelledError):
        transcribe.run(chunks(pcm), FakeModel(per_segment=1, **model_kwargs), window_seconds=10, cancel=cancel)
    assert time.monotonic() - started < 1.5


def test_a_slow_model_without_a_cancel_token_still_works_inline():
    pcm, _ = speech([0.5, 2.0], 4.0)
    done = transcribe.run(chunks(pcm), FakeModel(per_segment=1, segment_delay=0.05))
    assert len(done["segments"]) == 2


def test_an_error_in_the_model_reaches_the_job():
    class Broken:
        def transcribe(self, audio, **kwargs):
            raise ValueError("model exploded")

    pcm, _ = speech([0.5], 3.0)
    with pytest.raises(ValueError, match="model exploded"):
        transcribe.run(chunks(pcm), Broken(), cancel=Cancel())


def test_a_transcription_job_is_cancelled_within_a_second_or_two(store, monkeypatch):
    """Through the job queue, as the HTTP cancel route does: the job state is `cancelled`, not `running`."""
    media, asset, _ = store
    slow = FakeModel(per_segment=1, segment_delay=5.0)
    monkeypatch.setattr(transcribe, "require_available", lambda: None)
    monkeypatch.setattr(transcribe, "load_model", lambda name, models_dir: slow)
    queue = InProcessJobQueue()
    job_id = queue.submit("transcription", job_work.transcribe_work(media, asset.id))
    time.sleep(0.5)
    assert queue.status(job_id).state == "running"
    started = time.monotonic()
    info = cancel_job(queue, job_id)
    assert info.state == "cancelled" and time.monotonic() - started < 2.0
    assert transcribe.load(media, asset.id) is None
    queue.shutdown()


def test_cancel_is_seen_while_the_model_loads(store):
    media, asset, _ = store
    cancel = Cancel()
    _fire_after(cancel, 0.3)

    def slow_factory(name, models_dir):
        time.sleep(5.0)
        return FakeModel()

    started = time.monotonic()
    with pytest.raises(JobCancelledError):
        transcribe.transcribe(media, asset.id, model_factory=slow_factory, cancel=cancel)
    assert time.monotonic() - started < 1.5
