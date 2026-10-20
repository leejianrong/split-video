"""The media report (SPL-44): shape, size bound on a 3 hour recording, each signal absent, bucket math, the
degradation order, and `prepare` on a real short clip. Signals are planted (tests/fake_signals.py)."""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest
from fake_signals import make_store, plant

from split_video import assets, media_report, time_index
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.ffmpeg import MediaInfo
from split_video.jobs import JobCancelledError
from split_video.local_media_store import LocalMediaStore
from split_video.media_report import MediaReportError, encoded_size
from split_video.media_report import media_report as report
from split_video.media_store import InMemoryMediaStore

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "media_report" / "report_300s_30.json"
THREE_HOURS = 10800.0
COLUMNS = [
    "audio_class",
    "audio_share",
    "loudness_mean_db",
    "loudness_max_db",
    "silence_fraction",
    "words",
    "words_per_minute",
    "scene_changes",
    "activity_mean",
    "activity_max",
    "screen_text_changed",
    "transcript",
    "screen_text",
]
TOP = [
    "version",
    "asset",
    "signals",
    "prepared",
    "limits",
    "notices",
    "buckets",
    "loudness",
    "regions",
    "silence_gaps",
    "scene_changes",
    "transcript",
]


@pytest.fixture(params=["memory", "local"])
def store(request, tmp_path):
    return make_store(request.param, tmp_path)


def test_golden_shape(store):
    asset = plant(store)
    doc = report(store, asset.id, bucket_seconds=30)
    assert list(doc) == TOP and doc["version"] == 1
    if not GOLDEN.exists():  # regenerate by deleting the file
        GOLDEN.write_text(json.dumps(doc, indent=1) + "\n")
    expected = json.loads(GOLDEN.read_text())
    for key in ("id", "size"):  # content-derived: they differ between the two stores
        expected["asset"][key] = doc["asset"][key]
    assert doc == expected
    assert list(doc["buckets"]) == ["seconds", "count", *COLUMNS]
    assert doc["notices"] == {"coarsened": None, "shrunk": None, "dropped": [], "truncated": []}
    assert doc["signals"]["missing"] == [] and doc["prepared"] is None


def test_planted_values_agree_with_the_time_index(store):
    asset = plant(store)
    doc = report(store, asset.id, bucket_seconds=30)
    b = doc["buckets"]
    assert b["audio_class"][3] == "speech" and b["audio_class"][0] == "music"  # 90-120 s: speech from 96 s
    assert b["words"][3] == 10 and b["transcript"][3].startswith("welcome everybody")
    assert b["loudness_max_db"][6] == -3.0 and b["scene_changes"][0] == 1 and b["scene_changes"][5] == 1
    assert b["screen_text_changed"][:6] == [1, 0, 0, 0, 1, 0] and b["screen_text"][4] == "def main():"
    index = time_index.TimeIndex.load(store, asset.id)
    for k in range(b["count"]):
        w = index.window(k * 30.0, (k + 1) * 30.0)
        assert b["words"][k] == w["speech"]["word_count"]
        assert b["audio_class"][k] == w["audio"]["class"]
    assert doc["regions"]["items"][1] == [96.0, 200.2, "speech"] or doc["regions"]["items"][1][2] == "speech"
    assert doc["scene_changes"]["items"] == [[20.0, 0.4], [150.0, 0.62]]
    assert doc["loudness"]["max_db"] == -3.0 and len(doc["loudness"]["curve"]) <= 60
    assert doc["transcript"]["words"] == 10 and doc["transcript"]["language"] == "en"
    assert ["welcome", 1] in doc["transcript"]["top_keywords"] and ["the", 1] not in doc["transcript"]["top_keywords"]


@pytest.mark.parametrize(
    ("skip", "columns", "parts", "signal"),
    [
        ("audio", ["audio_class", "audio_share"], ["regions"], "audio"),
        ("loudness", ["loudness_mean_db", "loudness_max_db"], ["loudness"], "loudness"),
        ("silence", ["silence_fraction"], ["silence_gaps"], "silence"),
        ("speech", ["words", "words_per_minute", "transcript"], ["transcript"], "speech"),
        ("scenes", ["scene_changes", "activity_mean", "activity_max"], ["scene_changes"], "scenes"),
        ("ocr", ["screen_text_changed", "screen_text"], [], "ocr"),
    ],
)
def test_each_signal_absent_in_turn(store, skip, columns, parts, signal):
    asset = plant(store, skip=(skip,))
    doc = report(store, asset.id)
    assert [m["signal"] for m in doc["signals"]["missing"]] == [signal]
    assert doc["signals"]["available"][signal] is False
    assert all(v for k, v in doc["signals"]["available"].items() if k != signal)
    assert all(doc["buckets"][c] is None for c in columns) and all(doc[p] is None for p in parts)
    others = [c for c in COLUMNS if c not in columns]
    assert all(doc["buckets"][c] is not None for c in others)


def test_nothing_stored_gives_hints_not_errors(store):
    asset = plant(store, skip=("audio", "loudness", "silence", "speech", "scenes", "ocr"))
    doc = report(store, asset.id)
    assert [m["signal"] for m in doc["signals"]["missing"]] == list(time_index.SIGNALS)
    assert all(doc["buckets"][c] is None for c in COLUMNS) and doc["buckets"]["count"] == 30
    assert "report <target> --prepare" in doc["signals"]["missing"][1]["how"]
    assert encoded_size(doc) < 3000


def test_include_leaves_the_rest_out(store):
    asset = plant(store)
    doc = report(store, asset.id, include="speech,audio")
    assert doc["signals"]["excluded"] == ["loudness", "silence", "scenes", "ocr"]
    assert doc["buckets"]["words"] is not None and doc["buckets"]["loudness_max_db"] is None
    assert doc["loudness"] is None and doc["scene_changes"] is None and doc["regions"] is not None
    assert doc["signals"]["available"]["loudness"] is True  # it exists, it was just left out
    with pytest.raises(MediaReportError, match="include"):
        report(store, asset.id, include=["nope"])
    with pytest.raises(MediaReportError):
        report(store, asset.id, include=[])


@pytest.mark.parametrize(
    ("duration", "bucket", "count"),
    [(20.0, 10.0, 2), (300.0, 10.0, 30), (1800.0, 10.0, 180), (5400.0, 30.0, 180), (THREE_HOURS, 60.0, 180)],
)
def test_default_bucket_math(duration, bucket, count):
    store = make_store("memory", None, duration=duration)
    asset = plant(store, duration=duration, skip=("audio", "speech", "scenes", "ocr", "loudness", "silence"))
    b = report(store, asset.id)["buckets"]
    assert b["seconds"] == bucket and b["count"] == count


def test_bucket_edges_and_explicit_sizes():
    store = make_store("memory", None, duration=305.5)
    asset = plant(store, duration=305.5, skip=("ocr",))
    doc = report(store, asset.id, bucket_seconds=100)
    assert doc["buckets"]["seconds"] == 100.0 and doc["buckets"]["count"] == 4  # the last one is 5.5 s long
    assert len(doc["buckets"]["audio_class"]) == 4 and doc["buckets"]["audio_class"][3] == "music"
    assert report(store, asset.id, buckets=7)["buckets"]["seconds"] == 44.0  # ceil(305.5 / 7)
    assert report(store, asset.id, buckets=1)["buckets"]["count"] == 1
    assert report(store, asset.id, bucket_seconds=5)["buckets"]["count"] == 62
    for bad in (4.9, 601, -1, True, "x"):
        with pytest.raises(MediaReportError):
            report(store, asset.id, bucket_seconds=bad)
    for bad in (0, 721, 1.5):
        with pytest.raises(MediaReportError):
            report(store, asset.id, buckets=bad)
    with pytest.raises(MediaReportError, match="not both"):
        report(store, asset.id, bucket_seconds=10, buckets=5)
    with pytest.raises(MediaReportError, match="max_bytes"):
        report(store, asset.id, max_bytes=100)
    with pytest.raises(assets.AssetNotFoundError):
        report(store, "a_0000000000000000")
    long = make_store("memory", None, duration=THREE_HOURS)
    big = plant(long, duration=THREE_HOURS, skip=("audio", "speech", "scenes", "ocr", "loudness", "silence"))
    with pytest.raises(MediaReportError, match="too many"):
        report(long, big.id, bucket_seconds=10)  # 1080 buckets


@pytest.fixture(scope="module")
def dense_store():
    """3 hours with every signal: 27k words, 22.5k classification frames, 540 cuts, OCR frames, silences."""
    store = make_store("memory", None, duration=THREE_HOURS)
    return store, plant(store, duration=THREE_HOURS, dense=True)


def test_three_hours_dense_stays_under_the_documented_bound(dense_store):
    store, asset = dense_store
    index = time_index.TimeIndex.load(store, asset.id)
    assert len(index.words) >= 26_000 and len(index.cuts) >= 535 and len(index.class_regions) > 300
    doc = report(store, asset.id)
    size = encoded_size(doc)
    assert size <= media_report.DEFAULT_MAX_BYTES == 40_000
    assert size < 30_000, size  # measured about 24 KB
    assert doc["buckets"]["seconds"] == 60.0 and doc["buckets"]["count"] == 180
    assert doc["notices"]["coarsened"] is None and doc["notices"]["dropped"] == []
    assert len(media_report.dumps(doc).encode()) == size
    for name in ("regions", "scene_changes"):
        assert doc[name]["count"] > len(doc[name]["items"]) and name in doc["notices"]["truncated"]
    assert doc["transcript"]["words"] >= 26_000


@pytest.mark.parametrize("budget", [40_000, 20_000, 12_000, 8_000, 6_000, 4_096])
def test_budget_is_never_exceeded_and_degrades_in_order(dense_store, budget):
    store, asset = dense_store
    doc = report(store, asset.id, bucket_seconds=60, max_bytes=budget)
    assert encoded_size(doc) <= budget and doc["limits"] == {"max_bytes": budget}


def test_degradation_order(dense_store):
    store, asset = dense_store
    snippets = report(store, asset.id, bucket_seconds=60, max_bytes=21_000)  # drops the widest snippets first
    assert snippets["notices"]["shrunk"] is not None and snippets["notices"]["coarsened"] is None
    assert snippets["notices"]["dropped"] == []
    assert max(len(t) for t in snippets["buckets"]["transcript"]) <= snippets["notices"]["shrunk"]["transcript_chars"]
    plain = report(store, asset.id, bucket_seconds=60, max_bytes=14_000)  # then no snippets at all
    assert plain["notices"]["dropped"] == ["transcript", "screen_text"] and plain["buckets"]["transcript"] is None
    assert plain["buckets"]["seconds"] == 60.0 and plain["notices"]["coarsened"] is None
    coarse = report(store, asset.id, bucket_seconds=60, max_bytes=7_000)  # then coarser buckets
    assert coarse["notices"]["coarsened"]["requested_bucket_seconds"] == 60.0
    assert coarse["buckets"]["seconds"] > 60.0 and coarse["notices"]["coarsened"]["reason"] == "max_bytes"
    tiny = report(store, asset.id, bucket_seconds=60, max_bytes=4_096)
    assert encoded_size(tiny) <= 4_096 and tiny["buckets"] is None or tiny["buckets"]["seconds"] <= 600.0
    assert tiny["asset"]["id"] == asset.id and tiny["signals"]["available"]["audio"] is True


def test_the_last_resort_is_a_bare_document(dense_store):
    store, asset = dense_store
    doc = report(store, asset.id, max_bytes=media_report.MIN_MAX_BYTES)
    assert encoded_size(doc) <= media_report.MIN_MAX_BYTES


def test_missing_hints_fit_the_smallest_budget():
    store = make_store("memory", None)
    asset = plant(store, skip=tuple(time_index.SIGNALS))
    doc = report(store, asset.id, max_bytes=media_report.MIN_MAX_BYTES, bucket_seconds=5)
    assert encoded_size(doc) <= media_report.MIN_MAX_BYTES and doc["asset"]["id"] == asset.id


# --- prepare: real ffmpeg on a short clip ----------------------------------------------------------------


@pytest.fixture
def clip_workspace(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    root = tmp_path / "ws"
    root.mkdir()
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
    cmd += ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=3", "-filter_complex", "[0][1]concat=n=2:v=0:a=1"]
    cmd += [str(root / "clip.wav")]
    subprocess.run(cmd, check=True, capture_output=True)
    return root


class Token:
    """A cancel token that can be fired from a progress callback."""

    def __init__(self):
        self.flag = False
        self._callbacks = []

    def is_cancelled(self):
        return self.flag

    def raise_if_cancelled(self):
        if self.flag:
            raise JobCancelledError()

    def on_cancel(self, callback):
        self._callbacks.append(callback)
        return lambda: None


def test_report_is_read_only_by_default(clip_workspace):
    media = LocalMediaStore(clip_workspace)
    asset = assets.register_path(media, "clip.wav")
    doc = report(media, asset.id)
    assert [m["signal"] for m in doc["signals"]["missing"]] == ["audio", "loudness", "silence", "speech"]
    assert media.load_cache(asset.path, "waveform") is None and media.load_cache(asset.path, "silence") is None
    assert media.load_cache(asset.path, "classification") is None


@pytest.mark.skipif(not classify.MODEL_PATH.exists(), reason="needs the YAMNet model (make fetch-model)")
def test_prepare_computes_loudness_silence_and_classification_for_real(clip_workspace):
    media = LocalMediaStore(clip_workspace)
    asset = assets.register_path(media, "clip.wav")
    seen = []
    doc = report(
        media, asset.id, bucket_seconds=5, prepare=True, on_progress=lambda done, total: seen.append((done, total))
    )
    assert doc["prepared"] == {
        "asset": asset.id,
        "prepared": ["loudness", "silence", "classification"],
        "already": [],
        "skipped": [],
    }
    assert [m["signal"] for m in doc["signals"]["missing"]] == ["speech"]  # transcription is never automatic
    assert seen[0] == (0, 1000) and seen[-1] == (1000, 1000) and [d for d, _ in seen] == sorted(d for d, _ in seen)
    b = doc["buckets"]
    assert b["loudness_max_db"][0] > -25 and b["loudness_max_db"][-1] < -60  # the tone, then digital silence
    assert b["silence_fraction"][-1] > 0.5 and b["audio_class"][0] is not None
    assert doc["loudness"]["max_db"] > -25 and doc["silence_gaps"]["count"] == 1  # the 3 s of silence
    assert WaveformCache(media, asset.path).stored_loudness() is not None and SilenceCache(media, asset.path).stored()
    again = report(media, asset.id, prepare=True)  # idempotent: nothing is made twice
    assert again["prepared"]["prepared"] == [] and again["prepared"]["already"] == [
        "loudness",
        "silence",
        "classification",
    ]


def test_prepare_without_the_model_skips_it_and_says_why(clip_workspace, monkeypatch):
    def missing(*args, **kwargs):
        raise classify.ModelNotFoundError("YAMNet model not found - run `make fetch-model`")

    monkeypatch.setattr(media_report.project, "run_analysis", missing)
    media = LocalMediaStore(clip_workspace)
    asset = assets.register_path(media, "clip.wav")
    result = media_report.prepare_signals(media, asset.id)
    assert result["prepared"] == ["loudness", "silence"]
    assert result["skipped"] == [
        {"signal": "classification", "reason": "YAMNet model not found - run `make fetch-model`"}
    ]


def test_cancelled_prepare_leaves_nothing_partial(clip_workspace, monkeypatch):
    def slow(media, source, cache, thresholds, on_progress=None, cancel=None):
        on_progress(1, 10)
        cancel.flag = True
        cancel.raise_if_cancelled()

    monkeypatch.setattr(media_report.project, "run_analysis", slow)
    media = LocalMediaStore(clip_workspace)
    asset = assets.register_path(media, "clip.wav")
    token = Token()
    with pytest.raises(JobCancelledError):
        media_report.prepare_signals(media, asset.id, cancel=token)
    assert media.load_cache(asset.path, "classification") is None
    assert ClassificationCache(media, asset.path).is_analyzed is False
    # what finished before the cancel is whole and usable; the rest is absent
    assert WaveformCache(media, asset.path).stored_loudness() is not None


def test_cancel_before_anything_starts_stores_nothing(clip_workspace):
    media = LocalMediaStore(clip_workspace)
    asset = assets.register_path(media, "clip.wav")
    other = Token()
    other.flag = True
    with pytest.raises(JobCancelledError):
        media_report.prepare_signals(media, asset.id, cancel=other)
    assert media.load_cache(asset.path, "waveform") is None and media.load_cache(asset.path, "silence") is None


def test_prepare_on_an_asset_without_audio_does_nothing():
    store = InMemoryMediaStore()
    store.add("mute.mp4", b"x", info=MediaInfo(duration=20.0, width=320, height=240, has_audio=False, fps=5.0))
    asset = assets.register_path(store, "mute.mp4")
    result = media_report.prepare_signals(store, asset.id)
    assert result["prepared"] == [] and [s["signal"] for s in result["skipped"]] == list(media_report.PREPARABLE)
    doc = report(store, asset.id, prepare=True)
    assert [m["signal"] for m in doc["signals"]["missing"]] == ["scenes", "ocr"]  # no audio signals are listed


# --- Checkpoint B polish (SPL-108): speech pauses and loud non-speech ----------------------------------------


def _classified(winner_at, db, duration=120.0):
    """A store whose classification has `winner_at(slot)` per YAMNet frame (0.48 s) and a 1 s loudness curve `db(s)`."""
    store = make_store("memory", None, duration=duration)
    asset = assets.register_path(store, "talk.mp4")
    store.save_cache(
        asset.path,
        "waveform",
        {"buckets": [[-0.1, 0.1]], "loudness": {"window_seconds": 1.0, "db": [db(s) for s in range(int(duration))]}},
    )
    slots = math.ceil(duration * classify.SAMPLE_RATE / classify.HOP_SAMPLES)
    frames = [{b: (0.9 if b == winner_at(i) else 0.05) for b in classify.BUCKET_LABELS} for i in range(slots)]
    regions = classify.rethreshold(frames, dict(classify.DEFAULT_THRESHOLDS), duration)
    ClassificationCache(store, asset.path).store_result(frames, duration, regions)
    return store, asset


def _quiet(_s):
    return -60.0


def test_regions_merge_speech_pieces_across_short_gaps_before_the_minimum():
    # 4 pieces of 8 frames (3.84 s) with a 1 frame (0.48 s) pause between them: each is under the 10 s minimum, the
    # merged 16.8 s region is not. A second speech piece 3.84 s later is a separate region.
    def winner(i):
        if 10 <= i < 45 and (i - 10) % 9 != 8:
            return "speech"
        return "speech" if 53 <= i < 79 else "silence_other"

    store, asset = _classified(winner, _quiet)
    doc = report(store, asset.id, bucket_seconds=30)
    regions = doc["regions"]
    assert regions["min_seconds"] == 10.0
    speech = [r for r in regions["items"] if r[2] == "speech"]
    assert speech == [[4.8, 21.6, "speech"], [25.4, 37.9, "speech"]]
    assert regions["seconds_by_class"]["speech"] == 27.8  # the merged pauses are not counted
    assert doc["buckets"]["audio_class"][0] == "speech"  # per-bucket data is not changed by the merge


def test_gap_merge_only_joins_the_same_class():
    store, asset = _classified(lambda i: "speech" if i < 30 else "music" if 31 <= i < 70 else "silence_other", _quiet)
    items = report(store, asset.id)["regions"]["items"]
    assert [r[2] for r in items if r[2] != "silence_other"] == ["speech", "music"]


def test_loud_silence_other_is_other_sound_and_quiet_stays_silence_other():
    # 0-30 s quiet, 30-60 s loud noise, 60-90 s quiet again: one silence_other classification region
    store, asset = _classified(lambda i: "silence_other", lambda s: -24.0 if 30 <= s < 60 else -60.0, duration=90.0)
    doc = report(store, asset.id, bucket_seconds=30)
    assert doc["buckets"]["audio_class"] == ["silence_other", "other_sound", "silence_other"]
    assert doc["regions"]["items"] == [
        [0.0, 30.0, "silence_other"],
        [30.0, 60.0, "other_sound", "silence_other"],
        [60.0, 90.0, "silence_other"],
    ]
    assert doc["regions"]["seconds_by_class"] == {"other_sound": 30.0, "silence_other": 60.0}
    index = time_index.TimeIndex.load(store, asset.id)
    loud = index.at(45, 10)["audio"]
    assert loud["class"] == "other_sound" and loud["raw_class"] == "silence_other"
    assert loud["regions"][0]["class"] == "other_sound" and loud["regions"][0]["raw_class"] == "silence_other"
    quiet = index.at(15, 10)["audio"]
    assert quiet["class"] == "silence_other" and quiet["raw_class"] is None and "raw_class" not in quiet["regions"][0]
    assert index.timeline(0, 90, 30)["points"][1]["audio"] == "other_sound"


def test_without_a_loudness_curve_silence_other_is_left_alone():
    store, asset = _classified(lambda i: "silence_other", lambda s: -10.0, duration=30.0)
    index = time_index.TimeIndex(
        assets.get_asset(store, asset.id), regions=ClassificationCache(store, asset.path).get_regions()
    )
    audio = index.at(15, 10)["audio"]
    assert audio["class"] == "silence_other" and audio["raw_class"] is None


def test_other_classes_are_never_relabelled():
    store, asset = _classified(lambda i: "speech", lambda s: -10.0, duration=30.0)
    audio = time_index.TimeIndex.load(store, asset.id).at(15, 10)["audio"]
    assert audio["class"] == "speech" and audio["raw_class"] is None
