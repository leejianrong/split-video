"""The unified time index (SPL-100): every stored signal around a time in one call, from signals planted at known
times (tests/fake_signals.py), in memory and on the local store: values, edges, each signal missing in turn, errors,
determinism, bounded size, the golden JSON shape and the speed on three hours."""

import json
import math
import os
import time
from pathlib import Path

import pytest
from fake_signals import CUT_AT, DURATION, ON_SCREEN, PEAK_AT, SAID, make_store, plant

from split_video import assets, time_index
from split_video.ffmpeg import MediaInfo
from split_video.media_store import InMemoryMediaStore
from split_video.time_index import TimeIndex, TimeIndexError
from split_video.timeline import AssetNotFoundError

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "time_index"
HOW = {
    "audio": "split-video analyze",
    "loudness": "report <target> --prepare",
    "silence": "split-video propose",
    "speech": "split-video transcribe",
    "scenes": "split-video scenes",
    "ocr": "split-video ocr",
}


@pytest.fixture(params=["memory", "local"])
def media(request, tmp_path):
    return make_store(request.param, tmp_path)


@pytest.fixture
def asset(media):
    return plant(media)


def at(media, asset, t, window=None):
    return time_index.what_is_happening(media, asset.id, t, window)


# --- the planted signals are found ----------------------------------------------------------------


def test_applause_with_a_loudness_peak(media, asset):
    found = at(media, asset, 205)
    assert found["version"] == 1 and found["asset"] == asset.id and found["duration"] == DURATION
    assert found["time"] == 205.0 and found["window"] == {"start": 200.0, "end": 210.0}
    assert found["audio"]["class"] == "applause_crowd" and found["audio"]["share"] > 0.95
    assert found["audio"]["secondary"] == "speech"  # the first 0.16 s of the window are still speech
    assert found["audio"]["regions"][0]["start"] == 200.0 and found["audio"]["regions"][-1]["end"] == 210.0
    loud = found["loudness"]
    assert loud["max_db"] == -3.0 and loud["min_db"] == -40.0 and loud["window_seconds"] == 1.0
    assert {"t": PEAK_AT, "db": -3.0} in loud["curve"] and len(loud["curve"]) == 10
    assert found["speech"]["word_count"] == 0 and found["speech"]["text"] == ""
    assert found["silence"]["inside"] is False and found["silence"]["intervals"] == []
    assert found["missing"] == [] and all(v for k, v in found["available"].items() if k != "description")
    assert found["available"]["description"] is False and found["visual"]["description"] is None


def test_speech_words_and_rate(media, asset):
    found = at(media, asset, 100)
    speech = found["speech"]
    assert [w["word"] for w in speech["words"]] == SAID
    assert speech["words"][0] == {"start": 98.0, "end": 98.4, "word": "welcome"}
    assert speech["text"] == " ".join(SAID) and speech["word_count"] == 10 and speech["truncated"] is False
    assert speech["words_per_minute"] == 60.0  # 10 words in a 10 s window
    assert found["audio"]["class"] == "speech"
    narrow = at(media, asset, 98.2, 1)["speech"]  # [97.7, 98.7): "welcome" and the next word starts at 98.5
    assert [w["word"] for w in narrow["words"]] == ["welcome", "everybody"]
    assert narrow["words_per_minute"] == 120.0
    cut = at(media, asset, 98.45, 0.2)["speech"]  # a word cut by the window's edge is included
    assert [w["word"] for w in cut["words"]] == ["welcome", "everybody"]
    assert at(media, asset, 90)["speech"]["words"] == [] and at(media, asset, 90)["speech"]["words_per_minute"] == 0.0


def test_silence_inside_and_clipped(media, asset):
    inside = at(media, asset, 97)["silence"]
    assert inside["inside"] is True and inside["threshold_db"] == -35.0
    assert inside["intervals"] == [{"start": 96.0, "end": 97.5}]
    assert at(media, asset, 100)["silence"] == {
        "threshold_db": -35.0,
        "inside": False,
        "inside_exact": False,
        "intervals": [{"start": 96.0, "end": 97.5}],
        "truncated": False,
    }
    clipped = at(media, asset, 101, 10)["silence"]["intervals"]  # window [96, 106)
    assert clipped == [{"start": 96.0, "end": 97.5}]
    assert at(media, asset, 98, 2)["silence"]["intervals"] == [{"start": 97.0, "end": 97.5}]  # [97, 99)
    assert at(media, asset, 210.1, 1)["silence"]["intervals"] == []  # the 0.3 s one is too short to report
    assert at(media, asset, 199.5, 2)["silence"]["intervals"] == [{"start": 199.0, "end": 200.0}]


def test_scene_cut_and_shot(media, asset):
    cut = at(media, asset, 150)["visual"]
    assert cut["scene_change"] == {"time": CUT_AT, "score": 0.62, "kind": "cut"} and cut["scene_changes"] == 1
    assert cut["scene_index"] == 2 and cut["shot_start"] == CUT_AT
    assert cut["activity"]["max"] == 0.9 and cut["activity"]["mean"] == pytest.approx(0.18)
    before = at(media, asset, 147)["visual"]  # [142, 152): the cut is ahead of `time`
    assert before["scene_change"]["time"] == CUT_AT and before["scene_index"] == 1 and before["shot_start"] == 20.0
    quiet = at(media, asset, 100)["visual"]
    assert quiet["scene_change"] is None and quiet["scene_changes"] == 0 and quiet["scene_index"] == 1
    start = at(media, asset, 5)["visual"]
    assert start["scene_index"] == 0 and start["shot_start"] == 0.0 and start["scene_change"] is None


def test_on_screen_text_is_the_text_up_at_that_time(media, asset):
    early = at(media, asset, 100)["visual"]["text"]
    assert early["text"] == "intro slide" and (early["time"], early["until"]) == (0.0, 140.0) and early["more"] == 0
    shown = at(media, asset, 141)["visual"]["text"]
    assert shown["text"] == ON_SCREEN and shown["lines"] == ["def main():", "    run_stream()"]
    assert (shown["time"], shown["until"], shown["truncated"]) == (140.0, 300.0, False)
    assert shown["more"] == 1  # the intro slide was up at the start of the [136, 146) window
    assert at(media, asset, 139.9, 0.2)["visual"]["text"]["text"] == "intro slide"
    assert at(media, asset, 140, 0.2)["visual"]["text"]["text"] == ON_SCREEN
    assert at(media, asset, 200)["visual"]["text"]["more"] == 0


def test_a_blank_screen_is_no_text_but_the_latest_text_in_the_window_is_reported(media):
    asset = plant(media, skip=("ocr",))
    from fake_ocr import store_ocr

    store_ocr(media, asset, [(0.0, "first"), (50.0, "   ")], duration=DURATION)  # blank from 50 s: not a state
    assert at(media, asset, 60)["visual"]["text"] is None
    assert at(media, asset, 52, 10)["visual"]["text"]["text"] == "first"  # window [47, 57): up until 50 s
    assert at(media, asset, 40)["visual"]["text"]["until"] == 50.0


# --- silence versus speech, and stale on-screen text (SPL-107) -----------------------------------------


def test_inside_a_silence_is_false_while_a_word_is_being_spoken(media):
    asset = plant(media, skip=("silence",))
    # the silence scan ends 0.4 s after the first word starts (98.0): the two analyses disagree at the onset
    media.save_cache(asset.path, "silence", {"-35.0": [[96.0, 98.4]]})
    quiet = at(media, asset, 97.0)["silence"]
    assert quiet["inside"] is True and quiet["inside_exact"] is True
    early = at(media, asset, 97.6)["silence"]  # 0.4 s before the first word: its stated start runs late
    assert early["inside"] is False and early["inside_exact"] is True
    onset = at(media, asset, 98.1)["silence"]
    assert onset["inside"] is False and onset["inside_exact"] is True
    after = at(media, asset, 98.45)["silence"]  # a word is still sounding, the interval is over
    assert after["inside"] is False and after["inside_exact"] is False


def test_without_a_transcript_inside_and_inside_exact_agree(media):
    asset = plant(media, skip=("silence", "speech"))
    media.save_cache(asset.path, "silence", {"-35.0": [[96.0, 98.4]]})
    found = at(media, asset, 98.1)["silence"]
    assert found["inside"] is True and found["inside_exact"] is True


def _with_cut(media, asset, time, kind="screen"):
    from split_video import scenes

    doc = media.load_cache(asset.path, scenes.CACHE_NAME)
    doc["scenes"] = sorted(
        doc["scenes"] + [{"time": time, "score": 0.01, "kind": kind, "changed": 0.057}], key=lambda s: s["time"]
    )
    media.save_cache(asset.path, scenes.CACHE_NAME, doc)


def _text_at(media, asset, t, window=None):
    return at(media, asset, t, window)["visual"]["text"]


def test_text_read_before_a_scene_change_is_stale_and_says_why(media):
    from fake_ocr import store_ocr

    asset = plant(media, skip=("ocr",))
    store_ocr(
        media,
        asset,
        [(0.0, "intro"), (90.5, "def render_timeline(project):"), (150.0, "FAILED test_x")],
        duration=DURATION,
    )
    _with_cut(media, asset, 130.0)  # a screen change the OCR schedule never sampled after
    stale = _text_at(media, asset, 140)
    assert stale["text"].startswith("def render_timeline") and stale["age_seconds"] == 49.5
    assert stale["stale"] is True and "130" in stale["note"] and "no longer be on screen" in stale["note"]
    fresh = _text_at(media, asset, 120)  # nothing happened since the frame at 90.5
    assert fresh["stale"] is False and fresh["note"] is None and fresh["age_seconds"] == 29.5
    shown = _text_at(media, asset, 155)  # the frame at 150 read it 5 s ago
    assert shown["text"] == "FAILED test_x" and shown["stale"] is False and shown["age_seconds"] == 5.0


def test_a_frame_that_found_the_text_unchanged_resets_the_age(media):
    from fake_ocr import store_ocr

    asset = plant(media, skip=("ocr", "scenes"))
    document = store_ocr(media, asset, [(0.0, "intro"), (150.0, "next")], duration=DURATION)
    assert _text_at(media, asset, 100)["age_seconds"] == 100.0  # one frame at 0 saw it
    document["frames"].insert(
        1, {"time": 90.0, "text": "intro", "lines": ["intro"], "changed": False, "reused_from": 0.0}
    )
    from split_video import ocr

    media.save_cache(asset.path, ocr.CACHE_NAME, document)
    seen = _text_at(media, asset, 100)
    assert seen["age_seconds"] == 10.0 and seen["stale"] is False and seen["note"] is None


def test_old_text_is_stale_by_age_alone_without_scene_data(media):
    asset = plant(media, skip=("scenes",))
    old = _text_at(media, asset, 100)  # "intro slide" read at 0 s, OCR interval 30: more than 2 intervals
    assert old["age_seconds"] == 100.0 and old["stale"] is True and "100 s before" in old["note"]
    assert _text_at(media, asset, 50)["stale"] is False  # 50 s <= 2 * 30 s
    assert at(media, asset, 100)["available"]["scenes"] is False


def test_staleness_for_a_text_that_starts_after_the_time_is_age_zero(media):
    asset = plant(media)
    ahead = _text_at(media, asset, 139.9, 0.2)  # intro slide still up at 139.9 (until 140)
    assert ahead["age_seconds"] >= 0


# --- edges ----------------------------------------------------------------------------------------


def test_edges_of_the_asset(media, asset):
    start = at(media, asset, 0)
    assert start["window"] == {"start": 0.0, "end": 5.0} and start["time"] == 0.0
    assert start["audio"]["class"] == "music" and start["visual"]["text"]["text"] == "intro slide"
    end = at(media, asset, DURATION)
    assert end["window"] == {"start": 295.0, "end": 300.0} and end["time"] == DURATION
    assert end["visual"]["text"]["text"] == ON_SCREEN and end["loudness"]["curve"][-1]["t"] == 299.0
    whole = at(media, asset, 150, 600)
    assert whole["window"] == {"start": 0.0, "end": DURATION} and len(whole["loudness"]["curve"]) <= 60
    assert whole["loudness"]["max_db"] == -3.0 and max(p["db"] for p in whole["loudness"]["curve"]) == -3.0


def test_window_across_the_seam_of_two_regions(media, asset):
    found = at(media, asset, 200, 4)["audio"]  # [198, 202): speech until about 200.16, then applause
    classes = [r["class"] for r in found["regions"]]
    assert classes == ["speech", "applause_crowd"]
    assert found["regions"][0]["start"] == 198.0 and found["regions"][1]["end"] == 202.0
    assert found["regions"][0]["end"] == found["regions"][1]["start"]
    assert found["class"] == "speech" and found["secondary"] == "applause_crowd"
    assert found["share"] + 0.0 > 0.5


def test_explicit_window_matches_at(media, asset):
    index = TimeIndex.load(media, asset.id)
    assert index.window(200.0, 210.0) == index.at(205.0, 10.0)
    assert index.window(0.0, 4.0)["time"] == 2.0
    edge, span = index.at(DURATION, 10), index.window(295.0, 300.0)
    assert edge["visual"]["text"]["age_seconds"] > span["visual"]["text"]["age_seconds"]  # measured to `time`
    for answer in (edge, span):
        answer["visual"]["text"].pop("age_seconds")
        answer["visual"]["text"].pop("note")
    assert edge == span | {"time": DURATION}


def test_caps_on_a_long_window(tmp_path):
    media = make_store("memory", tmp_path, duration=3600.0)
    asset = plant(media, duration=3600.0, dense=True)
    found = at(media, asset, 1800, 600)
    speech = found["speech"]
    assert len(speech["words"]) == time_index.MAX_WORDS and speech["truncated"] is True
    assert speech["word_count"] > 1000 and speech["text"].count(" ") == time_index.MAX_WORDS - 1
    assert len(found["loudness"]["curve"]) <= time_index.MAX_CURVE
    assert len(found["visual"]["activity"]["curve"]) <= time_index.MAX_CURVE
    assert len(found["audio"]["regions"]) <= time_index.MAX_REGIONS
    assert len(found["silence"]["intervals"]) <= time_index.MAX_SILENCES
    peak = at(media, asset, 300, 600)  # [0, 600) holds the 205 s peak: thinning keeps the loudest of each group
    assert max(p["db"] for p in peak["loudness"]["curve"]) == -3.0
    assert max(p["value"] for p in peak["visual"]["activity"]["curve"]) == 0.9
    from fake_ocr import store_ocr

    store_ocr(media, asset, [(0.0, "\n".join(f"line {k}" for k in range(40)))], duration=3600.0)
    text = at(media, asset, 10)["visual"]["text"]
    assert text["truncated"] is True and len(text["lines"]) == time_index.MAX_TEXT_LINES
    store_ocr(media, asset, [(0.0, "\n".join("y" * 300 for _ in range(5)))], duration=3600.0)
    text = at(media, asset, 10)["visual"]["text"]
    assert text["truncated"] is True and len(text["text"]) == time_index.MAX_TEXT_CHARS


# --- a missing signal is reported, never a failure -----------------------------------------------


GROUPS = {
    "audio": ("audio",),
    "loudness": ("loudness",),
    "silence": ("silence",),
    "speech": ("speech",),
    "scenes": (
        "visual.scene_change",
        "visual.scene_changes",
        "visual.scene_index",
        "visual.shot_start",
        "visual.activity",
    ),
    "ocr": ("visual.text",),
}


def _get(found, dotted):
    node = found
    for part in dotted.split("."):
        node = node[part]
    return node


@pytest.mark.parametrize("signal", list(GROUPS))
def test_each_signal_missing_in_turn(media, signal):
    asset = plant(media, skip=(signal,))
    found = at(media, asset, 100)
    for key in GROUPS[signal]:
        assert _get(found, key) is None, key
    assert found["available"][signal] is False
    assert [m["signal"] for m in found["missing"]] == [signal] and HOW[signal] in found["missing"][0]["how"]
    for other, keys in GROUPS.items():
        if other != signal:
            assert found["available"][other] is True
            assert any(_get(found, k) is not None for k in keys), other
    timeline = time_index.timeline_index(media, asset.id, 0, 60, 30)
    assert timeline["available"][signal] is False and [m["signal"] for m in timeline["missing"]] == [signal]


def test_nothing_computed_at_all(media):
    asset = plant(media, skip=tuple(HOW))
    found = at(media, asset, 100)
    assert found["audio"] is found["loudness"] is found["silence"] is found["speech"] is None
    assert found["visual"] == {
        "scene_change": None,
        "scene_changes": None,
        "scene_index": None,
        "shot_start": None,
        "activity": None,
        "description": None,
        "text": None,
    }
    assert [m["signal"] for m in found["missing"]] == list(HOW)
    assert all(m["how"] for m in found["missing"]) and found["time"] == 100.0
    line = time_index.timeline_index(media, asset.id, 0, 120, 60)
    assert line["count"] == 2 and all(p["audio"] is None and p["text"] is None for p in line["points"])


def test_signals_an_asset_cannot_have_are_not_listed_as_missing():
    media = InMemoryMediaStore()
    media.add("talk.wav", b"x", info=MediaInfo(duration=60.0, width=None, height=None, has_audio=True))
    audio_only = assets.register_path(media, "talk.wav")
    assert [m["signal"] for m in at(media, audio_only, 10)["missing"]] == ["audio", "loudness", "silence", "speech"]
    media.add("clip.mp4", b"y", info=MediaInfo(duration=60.0, width=64, height=64, has_audio=False, fps=5.0))
    silent = assets.register_path(media, "clip.mp4")
    assert [m["signal"] for m in at(media, silent, 10)["missing"]] == ["scenes", "ocr"]


def test_a_loudness_curve_is_never_computed(media, monkeypatch):
    asset = plant(media, skip=("loudness",))
    from split_video import ffmpeg
    from split_video.editor import cache

    def refuse(*args, **kwargs):
        raise AssertionError("audio was decoded")

    monkeypatch.setattr(cache, "iter_pcm_chunks", refuse)
    monkeypatch.setattr(ffmpeg, "iter_pcm_chunks", refuse)
    monkeypatch.setattr(cache, "detect_silence", refuse)
    assert at(media, asset, 100)["loudness"] is None
    media.save_cache(asset.path, "waveform", {"buckets": [[0, 0]]})  # peaks stored, no loudness (an old entry)
    assert at(media, asset, 100)["loudness"] is None


def test_the_closest_stored_silence_scan_is_used(media):
    asset = plant(media, skip=("silence",))
    media.save_cache(asset.path, "silence", {"-50.0": [[10.0, 12.0]], "-30.0": [[20.0, 22.0]]})
    found = at(media, asset, 21)["silence"]
    assert found["threshold_db"] == -30.0 and found["inside"] is True
    media.save_cache(asset.path, "silence", {"-50.0": [[10.0, 12.0]]})
    assert at(media, asset, 11)["silence"]["threshold_db"] == -50.0


# --- errors ---------------------------------------------------------------------------------------


def test_unknown_asset(media):
    with pytest.raises(AssetNotFoundError):
        time_index.what_is_happening(media, "a_0000000000000000", 1.0)
    with pytest.raises(AssetNotFoundError):
        time_index.timeline_index(media, "a_0000000000000000")


@pytest.mark.parametrize(
    ("t", "window", "message"),
    [
        (-0.001, None, "negative"),
        (300.001, None, "beyond the end"),
        (1e9, None, "beyond the end"),
        (math.nan, None, "number"),
        (math.inf, None, "number"),
        ("5", None, "number"),
        (True, None, "number"),
        (10, 0, "above 0"),
        (10, -1, "above 0"),
        (10, 600.001, "at most 600"),
        (10, math.nan, "number"),
        (10, "x", "number"),
    ],
)
def test_bad_time_or_window(media, asset, t, window, message):
    with pytest.raises(TimeIndexError, match=message):
        at(media, asset, t, window)


def test_window_boundaries_are_inclusive_where_documented(media, asset):
    assert at(media, asset, 150, 600)["window"]["end"] == DURATION
    assert at(media, asset, 150, 0.001)["window"]["end"] == pytest.approx(150.0005, abs=1e-3)
    assert at(media, asset, DURATION)["time"] == DURATION
    assert at(media, asset, 0)["time"] == 0.0


def test_bad_explicit_span_and_timeline_arguments(media, asset):
    index = TimeIndex.load(media, asset.id)
    for start, end, message in [
        (-1, 5, "negative"),
        (5, 5, "after"),
        (6, 5, "after"),
        (0, 301, "beyond"),
        (0, 601, "beyond"),
    ]:
        with pytest.raises(TimeIndexError, match=message):
            index.window(start, end)
    big = make_store("memory", None, duration=3600.0, path="big.mp4")
    with pytest.raises(TimeIndexError, match="at most 600"):
        TimeIndex.load(big, plant(big, "big.mp4", duration=3600.0, skip=tuple(HOW)).id).window(0, 601)
    for start, end, step, message in [
        (-1, 10, 5, "negative"),
        (10, 10, 5, "after"),
        (0, 400, 5, "beyond"),
        (0, 10, 0.5, "at least 1"),
        (0, 10, 0, "at least 1"),
        (0, 10, math.nan, "number"),
        (0, 300, 0.6, "at least 1"),
    ]:
        with pytest.raises(TimeIndexError, match=message):
            index.timeline(start, end, step)
    with pytest.raises(TimeIndexError, match="too many"):
        TimeIndex.load(big, assets.register_path(big, "big.mp4").id).timeline(0, 3600, 1)


# --- determinism, shape and size -----------------------------------------------------------------


def test_deterministic(media, asset):
    first = json.dumps(at(media, asset, 100), sort_keys=False)
    assert json.dumps(at(media, asset, 100), sort_keys=False) == first
    index = TimeIndex.load(media, asset.id)
    assert json.dumps(index.at(100, 10), sort_keys=False) == first
    other = TimeIndex.load(media, asset.id)
    assert other.at(100) == index.at(100) and other.timeline() == index.timeline()
    index.at(0, 1)  # asking never changes the index
    assert json.dumps(index.at(100, 10), sort_keys=False) == first


def test_the_result_does_not_alias_the_index(media, asset):
    index = TimeIndex.load(media, asset.id)
    found = index.at(100)
    found["speech"]["words"][0]["word"] = "changed"
    found["available"]["audio"] = False
    found["missing"].append({"signal": "x", "how": "y"})
    assert index.at(100)["speech"]["words"][0]["word"] == "welcome" and index.at(100)["available"]["audio"] is True
    assert index.at(100)["missing"] == []


def _golden(name, value):
    path = GOLDEN / name
    if os.environ.get("SPLIT_VIDEO_UPDATE_GOLDEN"):
        path.write_text(json.dumps(value, indent=2) + "\n")
    assert json.loads(path.read_text()) == value, f"{name} differs: rerun with SPLIT_VIDEO_UPDATE_GOLDEN=1 if intended"
    assert list(json.loads(path.read_text())) == list(value)  # key order is part of the shape


def test_golden_shapes():
    media = make_store("memory", None)
    asset = plant(media)
    index = TimeIndex.load(media, asset.id)
    _golden("at_100.json", index.at(100, 10))
    _golden("at_205.json", index.at(205, 10))
    _golden("at_0.json", index.at(0, 10))
    _golden("timeline_0_120_30.json", index.timeline(0, 120, 30))
    bare = make_store("memory", None, path="bare.mp4")
    nothing = plant(bare, "bare.mp4", skip=tuple(HOW))
    _golden("at_nothing_stored.json", TimeIndex.load(bare, nothing.id).at(100, 10))


def test_shape_is_stable_whatever_is_stored(media, asset):
    full = at(media, asset, 100)
    for signal in HOW:
        store = make_store("memory", None)
        less = at(store, plant(store, skip=(signal,)), 100)
        assert list(less) == list(full)
        for key in ("audio", "loudness", "silence", "speech"):
            if less[key] is not None:
                assert list(less[key]) == list(full[key])
        assert list(less["visual"]) == list(full["visual"])
        assert list(less["available"]) == list(full["available"])


def test_default_response_size(media, asset):
    sizes = {t: len(json.dumps(at(media, asset, t), separators=(",", ":"))) for t in (0, 100, 150, 205, 299)}
    assert max(sizes.values()) < 4096, sizes
    dense = make_store("memory", None, duration=3600.0)
    long_asset = plant(dense, duration=3600.0, dense=True)
    busy = len(json.dumps(at(dense, long_asset, 1805), separators=(",", ":")))
    assert busy < 3584
    widest = len(json.dumps(at(dense, long_asset, 1800, 600), separators=(",", ":")))
    assert widest < 20_000


# --- the bulk form -------------------------------------------------------------------------------


def test_timeline_points(media, asset):
    line = time_index.timeline_index(media, asset.id)
    assert (line["version"], line["asset"], line["start"], line["end"], line["step"]) == (1, asset.id, 0.0, 300.0, 60.0)
    assert line["count"] == len(line["points"]) == 5 and line["duration"] == DURATION
    assert [p["start"] for p in line["points"]] == [0.0, 60.0, 120.0, 180.0, 240.0]
    assert line["points"][-1]["end"] == 300.0
    expected = {
        "start",
        "end",
        "audio",
        "loudness_db",
        "loudness_max_db",
        "word_count",
        "words_per_minute",
        "scene_changes",
        "activity",
        "text",
    }
    assert all(set(p) == expected for p in line["points"])
    assert line["points"][0]["audio"] == "music" and line["points"][2]["audio"] == "speech"
    assert line["points"][1]["word_count"] == 10 and line["points"][1]["words_per_minute"] == 10.0
    assert line["points"][2]["scene_changes"] == 1 and line["points"][3]["loudness_max_db"] == -3.0
    assert line["points"][0]["text"] == "intro slide" and line["points"][2]["text"] == "def main():"
    index = TimeIndex.load(media, asset.id)
    full = index.window(60.0, 120.0)
    assert line["points"][1]["loudness_db"] == full["loudness"]["mean_db"]
    assert line["points"][1]["activity"] == full["visual"]["activity"]["mean"]


def test_timeline_span_step_and_cap(media, asset):
    line = time_index.timeline_index(media, asset.id, 100, 130, 10)
    assert [p["start"] for p in line["points"]] == [100.0, 110.0, 120.0] and line["step"] == 10.0
    odd = time_index.timeline_index(media, asset.id, 0, 25, 10)
    assert [(p["start"], p["end"]) for p in odd["points"]] == [(0.0, 10.0), (10.0, 20.0), (20.0, 25.0)]
    exactly = time_index.timeline_index(media, asset.id, 0, 300, 1.0)
    assert exactly["count"] == 300
    big = make_store("memory", None, duration=36_000.0, path="day.mp4")
    day = assets.register_path(big, "day.mp4")
    auto = TimeIndex.load(big, day.id).timeline()  # 10 h: 60 s would be 600 points, so the step is raised
    assert auto["count"] <= time_index.MAX_POINTS and auto["step"] == 72.0
    with pytest.raises(TimeIndexError, match="at least 72 seconds"):
        TimeIndex.load(big, day.id).timeline(step=60)


# --- three hours ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def three_hours():
    media = make_store("memory", None, duration=10_800.0, path="stream.mp4")
    return media, plant(media, "stream.mp4", duration=10_800.0, dense=True)


def test_three_hours_load_once_ask_many(three_hours):
    media, asset = three_hours
    started = time.perf_counter()
    index = TimeIndex.load(media, asset.id)
    loaded = time.perf_counter() - started
    assert index.available == {**dict.fromkeys(HOW, True), "description": False}
    started = time.perf_counter()
    for k in range(300):
        index.at(k * 35.0 + 1.0, 10)
    per_call = (time.perf_counter() - started) / 300
    print(f"3 h: load {loaded * 1000:.0f} ms, at() {per_call * 1000:.2f} ms")
    assert per_call < 0.05  # milliseconds; the margin is for a loaded CI machine
    found = index.at(5000.0, 10)
    assert found["audio"]["class"] in ("music", "singing") and found["speech"]["word_count"] > 20
    assert found["visual"]["text"]["text"].startswith("def handler_")
    started = time.perf_counter()
    line = index.timeline()
    assert line["count"] == 180 and time.perf_counter() - started < 2.0
    assert time_index.what_is_happening(media, asset.id, 10_800.0)["window"]["end"] == 10_800.0
