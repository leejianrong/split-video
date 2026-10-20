"""Highlight suggestions (SPL-48): planted peaks and decoys in 3 hour recordings, stored through the engines' own
save paths (tests/fake_highlights.py). Everything here reads documents only."""

import time

import fake_highlights as fh
import pytest
from fake_signals import make_store

from split_video import highlights
from split_video.time_index import TimeIndex
from split_video.timeline import TimelineError


def _build(builder, duration, path):
    media = make_store("memory", None, duration=duration, path=path)
    asset = builder(media, path, duration=duration)
    return media, asset


@pytest.fixture(scope="module")
def gig():
    media, asset = _build(fh.gig, 10_800.0, "gig.mp4")
    return media, asset, TimeIndex.load(media, asset.id)


@pytest.fixture(scope="module")
def stream():
    media, asset = _build(fh.stream, 10_800.0, "stream.mp4")
    return media, asset, TimeIndex.load(media, asset.id)


@pytest.fixture(scope="module")
def mixed():
    media, asset = _build(fh.mixed, 3600.0, "mixed.mp4")
    return media, asset, TimeIndex.load(media, asset.id)


def _covers(candidate, peak, slack=0.5):
    return candidate["start"] <= peak[0] + slack and candidate["end"] >= peak[1] - slack


def _find(found, peak):
    return next((c for c in found["candidates"] if _covers(c, peak)), None)


def _check_shape(found, min_seconds=15, max_seconds=60, gap=10):
    last_end = -1e9
    for c in sorted(found["candidates"], key=lambda c: c["start"]):
        assert min_seconds - 1e-6 <= c["duration"] <= max_seconds + 1e-6, c
        assert c["end"] - c["start"] == pytest.approx(c["duration"], abs=1e-3)
        assert c["start"] >= last_end + gap - 1e-6, "candidates overlap or are closer than min_gap_seconds"
        assert 0.0 <= c["score"] <= 1.0
        last_end = c["end"]
    assert [c["rank"] for c in found["candidates"]] == list(range(1, len(found["candidates"]) + 1))
    scores = [c["score"] for c in found["candidates"]]
    assert scores == sorted(scores, reverse=True)


# --- the planted peaks --------------------------------------------------------------------------------------


def test_gig_peaks_are_the_top_candidates_ranked_above_the_decoys(gig):
    _, asset, index = gig
    found = highlights.suggest_from_index(index, preset="gig", count=8)
    assert found["version"] == 1 and found["asset"] == asset.id and found["preset"] == "gig"
    _check_shape(found)
    top = found["candidates"][: len(fh.GIG_PEAKS)]
    for peak in fh.GIG_PEAKS:
        hit = _find({"candidates": top}, peak)
        assert hit is not None, f"planted peak {peak} is not in the top {len(top)}"
        # boundaries snapped to the planted silences: the peak's own edges, not the window's
        assert hit["start"] == pytest.approx(peak[0], abs=0.01) and hit["end"] == pytest.approx(peak[1], abs=0.01)
        assert hit["snapped"]["start"] == "silence" and hit["snapped"]["end"] == "silence"
        assert any(r["signal"] == "applause" and "applause" in r["note"] for r in hit["reasons"])
    decoys = [c for c in found["candidates"][len(fh.GIG_PEAKS) :]]
    assert decoys and all(c["score"] < top[-1]["score"] for c in decoys)
    for quiet in ((4000.0, 4100.0),):  # the quiet stretch never makes the list
        assert not any(c["start"] < quiet[1] and c["end"] > quiet[0] for c in found["candidates"])


def test_stream_peaks_are_the_top_candidates_ranked_above_the_decoys(stream):
    _, _, index = stream
    found = highlights.suggest_from_index(index, preset="stream", count=6)
    _check_shape(found)
    top = found["candidates"][: len(fh.STREAM_PEAKS)]
    for peak in fh.STREAM_PEAKS:
        hit = _find({"candidates": top}, peak)
        assert hit is not None, f"planted peak {peak} is not in the top {len(top)}"
        assert hit["start"] == pytest.approx(peak[0], abs=0.01) and hit["end"] == pytest.approx(peak[1], abs=0.01)
        notes = " ".join(r["note"] for r in hit["reasons"])
        assert "FAILED" in notes and "words/s" in notes and "scene/screen changes" in notes
        assert "FAILED" in hit["title_hint"] and "FAILED" in hit["ocr_excerpt"] and hit["transcript_excerpt"]
        assert hit["score"] > 0.5
    assert all(c["score"] < 0.2 for c in found["candidates"][len(fh.STREAM_PEAKS) :])
    assert not any(c["start"] < 4200.0 and c["end"] > 4000.0 for c in found["candidates"])  # the quiet stretch


def test_score_parts_add_up_and_reasons_are_human_readable(stream):
    _, _, index = stream
    found = highlights.suggest_from_index(index, preset="stream", count=2)
    for c in found["candidates"]:
        assert sum(c["score_parts"].values()) == pytest.approx(c["score"], abs=0.01)
        for r in c["reasons"]:
            assert set(r) == {"signal", "weight", "value", "note"} and r["note"]
            assert r["signal"] in highlights.WEIGHT_SIGNALS and 0 <= r["value"] <= 1


def test_results_are_deterministic(gig):
    _, _, index = gig
    a = highlights.suggest_from_index(index, preset="gig", count=10)
    b = highlights.suggest_from_index(index, preset="gig", count=10)
    assert highlights.dumps(a) == highlights.dumps(b)


def test_non_overlap_holds_with_a_wide_gap_and_many_candidates(gig):
    _, _, index = gig
    found = highlights.suggest_from_index(index, preset="default", count=50, min_gap_seconds=120)
    _check_shape(found, gap=120)
    assert len(found["candidates"]) > 3


def test_candidates_are_usable_seconds_on_the_assets_timeline(gig):
    _, _, index = gig
    for c in highlights.suggest_from_index(index, preset="gig")["candidates"]:
        assert 0 <= c["start"] < c["end"] <= index.duration
        assert isinstance(c["start"], float) and isinstance(c["end"], float)


# --- presets, weights and keywords ---------------------------------------------------------------------------


def test_presets_rank_the_two_kinds_of_peak_in_opposite_orders(mixed):
    _, _, index = mixed
    gig_first = highlights.suggest_from_index(index, preset="gig", count=2)["candidates"]
    stream_first = highlights.suggest_from_index(index, preset="stream", count=2)["candidates"]
    assert _covers(gig_first[0], (1000.0, 1030.0)) and _covers(gig_first[1], (2000.0, 2040.0))
    assert _covers(stream_first[0], (2000.0, 2040.0)) and _covers(stream_first[1], (1000.0, 1030.0))
    lecture = highlights.suggest_from_index(index, preset="lecture", count=2)["candidates"]
    assert _covers(lecture[0], (2000.0, 2040.0))  # slide-like changes and on-screen text, not applause


def test_a_weights_override_changes_the_ranking(mixed):
    _, _, index = mixed
    flipped = highlights.suggest_from_index(
        index, preset="gig", count=2, weights={"speech": 6, "scene": 6, "ocr": 6, "activity": 4, "applause": 0.1}
    )
    assert _covers(flipped["candidates"][0], (2000.0, 2040.0))
    assert flipped["params"]["weights"]["speech"] == 6 and flipped["params"]["weights"]["applause"] == 0.1
    assert flipped["params"]["weights"]["music"] == highlights.PRESETS["gig"]["weights"]["music"]  # the rest stays
    off = highlights.suggest_from_index(index, preset="gig", count=2, weights={"applause": 0, "loudness": 0})
    assert "applause" not in off["signals"]["used"] and "loudness" not in off["signals"]["used"]


def test_weights_can_be_given_as_json_or_a_compact_string():
    assert highlights.parse_weights("speech:2,applause=0.5") == {"speech": 2.0, "applause": 0.5}
    assert highlights.parse_weights('{"speech": 2}') == {"speech": 2}
    assert highlights.parse_weights("") is None and highlights.parse_weights(None) is None
    for bad in ("speech", "speech:x", "{nope", "[1]"):
        with pytest.raises(highlights.HighlightsError):
            highlights.parse_weights(bad)
    assert highlights.parse_keywords("FAILED, error,failed") == ["FAILED", "error"]
    assert highlights.parse_keywords(None) is None and highlights.parse_keywords([]) == []


def test_weights_are_validated_with_typed_errors(gig):
    _, _, index = gig
    with pytest.raises(highlights.HighlightsError, match="unknown weight signal 'bass'.*speech, loudness, applause"):
        highlights.suggest_from_index(index, weights={"bass": 1})
    with pytest.raises(highlights.HighlightsError, match="must not be negative"):
        highlights.suggest_from_index(index, weights={"speech": -1})
    for bad in (float("nan"), "x", True, None):
        with pytest.raises(highlights.HighlightsError, match="must be a number"):
            highlights.suggest_from_index(index, weights={"speech": bad})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"preset": "karaoke"},
        {"count": 0},
        {"count": 1000},
        {"count": 1.5},
        {"min_seconds": 0},
        {"min_seconds": 30, "max_seconds": 20},
        {"target_seconds": 100},
        {"target_seconds": 5},
        {"start": -1},
        {"start": 100, "end": 50},
        {"end": 99999},
        {"min_gap_seconds": -1},
        {"min_seconds": "soon"},
    ],
)
def test_bad_arguments_are_typed_errors(gig, kwargs):
    _, _, index = gig
    with pytest.raises(highlights.HighlightsError):
        highlights.suggest_from_index(index, **kwargs)


def test_missing_signals_are_omitted_and_the_rest_renormalised(mixed):
    media, asset, index = mixed
    full = highlights.suggest_from_index(index, preset="stream", count=2)
    assert full["signals"]["used"] == list(highlights.POSITIVE)
    lean_media, lean_asset = _build(
        lambda m, p, duration: fh.mixed(m, p, skip=("scenes", "ocr", "audio"), duration=duration), 3600.0, "lean.mp4"
    )
    lean = highlights.suggest_highlights(lean_media, lean_asset.id, preset="stream", count=2)
    assert lean["signals"]["used"] == ["speech", "loudness", "keywords"]  # keywords: still spoken words
    assert not lean["signals"]["available"]["scenes"] and not lean["signals"]["available"]["ocr"]
    assert {m["signal"] for m in lean["signals"]["missing"]} == {"audio", "scenes", "ocr"}
    assert any("no stored data for" in n for n in lean["notes"])
    assert _covers(lean["candidates"][0], (2000.0, 2040.0))  # the speech burst still surfaces
    weights = [r["weight"] for r in lean["candidates"][0]["reasons"] if r["signal"] != "silence"]
    assert (
        sum(weights) <= 1.0 + 1e-6
        and sum(highlights.PRESETS["stream"]["weights"][s] for s in lean["signals"]["used"]) > 0
    )
    for c in lean["candidates"]:
        assert set(c["score_parts"]) <= {"speech", "loudness", "keywords", "silence"}


def test_audio_only_signals_still_score(mixed):
    media, asset = _build(
        lambda m, p, duration: fh.mixed(
            m, p, skip=("scenes", "ocr", "speech", "loudness", "silence"), duration=duration
        ),
        3600.0,
        "audio.mp4",
    )
    found = highlights.suggest_highlights(media, asset.id, preset="gig", count=1)
    assert found["signals"]["used"] == ["applause", "music", "noise"] and _covers(
        found["candidates"][0], (1000.0, 1030.0)
    )
    # without a silence scan the quiet silence_other regions are the gaps: the edges still snap to them
    assert found["candidates"][0]["snapped"]["start"] == "silence"


def test_keywords_boost_the_windows_that_contain_them(mixed):
    _, _, index = mixed
    plain = highlights.suggest_from_index(index, preset="default", count=2, keywords=[])
    boosted = highlights.suggest_from_index(index, preset="default", count=2, keywords=["failed"])  # case-insensitive
    assert "keywords" not in plain["signals"]["used"] and "keywords" in boosted["signals"]["used"]
    burst = _find(boosted, (2000.0, 2040.0))
    assert burst["score"] > _find(plain, (2000.0, 2040.0))["score"]
    assert any(r["signal"] == "keywords" and "'failed'" in r["note"] for r in burst["reasons"])
    assert boosted["params"]["keywords"] == ["failed"]
    # spoken keywords count too
    spoken = highlights.suggest_from_index(index, preset="default", count=2, keywords=["w7"])
    assert spoken["params"]["keywords"] == ["w7"] and "keywords" in spoken["signals"]["used"]
    # the stream preset brings its own, and an explicit empty list switches them off
    assert highlights.suggest_from_index(index, preset="stream")["params"]["keywords"][0] == "FAILED"
    assert highlights.suggest_from_index(index, preset="stream", keywords=[])["params"]["keywords"] == []


def test_the_span_limits_where_to_look(mixed):
    _, _, index = mixed
    found = highlights.suggest_from_index(index, preset="gig", count=5, start=1500, end=3000)
    assert found["params"]["start"] == 1500.0 and found["params"]["end"] == 3000.0
    assert found["candidates"] and all(1500 <= c["start"] and c["end"] <= 3000 for c in found["candidates"])
    assert _covers(found["candidates"][0], (2000.0, 2040.0))


def test_snap_false_keeps_the_scored_windows(gig):
    _, _, index = gig
    found = highlights.suggest_from_index(index, preset="gig", count=3, snap=False, target_seconds=30)
    assert found["params"]["snap"] is False
    for c in found["candidates"]:
        assert c["duration"] == pytest.approx(30.0) and c["start"] % 5 == 0
        assert c["snapped"]["start"] is None and c["snapped"]["end"] is None
    snapped = highlights.suggest_from_index(index, preset="gig", count=3)
    assert snapped["candidates"][2]["duration"] == pytest.approx(40.0)  # the 40 s peak is taken whole when snapping


def test_durations_follow_min_and_max_seconds(stream):
    _, _, index = stream
    found = highlights.suggest_from_index(index, preset="stream", count=4, min_seconds=20, max_seconds=35)
    _check_shape(found, 20, 35)
    assert found["params"]["target_seconds"] == 30.0
    short = highlights.suggest_from_index(index, preset="stream", count=2, min_seconds=5, max_seconds=12)
    _check_shape(short, 5, 12)
    assert short["params"]["target_seconds"] == 12.0  # 30 clamped to the maximum


def test_a_span_shorter_than_min_seconds_is_used_whole(mixed):
    _, _, index = mixed
    found = highlights.suggest_from_index(index, preset="gig", start=1000, end=1010)
    assert found["candidates"] and found["candidates"][0]["duration"] == pytest.approx(10.0)
    assert any("shorter than min_seconds" in n for n in found["notes"])


# --- no signals, unknown asset, cost --------------------------------------------------------------------------


def test_no_signals_is_a_typed_error_naming_what_to_run():
    media = make_store("memory", None, duration=600.0, path="empty.mp4")
    from split_video import assets

    asset = assets.register_path(media, "empty.mp4")
    with pytest.raises(highlights.NoSignalsError) as exc:
        highlights.suggest_highlights(media, asset.id)
    for word in ("report <target> --prepare", "transcribe", "scenes", "ocr"):
        assert word in str(exc.value)
    assert isinstance(exc.value, highlights.HighlightsError)
    # a silence scan alone cannot score a window
    media2 = make_store("memory", None, duration=600.0, path="s.mp4")
    asset2 = fh.gig(media2, "s.mp4", skip=("audio", "loudness", "speech", "scenes", "ocr"), duration=600.0)
    with pytest.raises(highlights.NoSignalsError):
        highlights.suggest_highlights(media2, asset2.id)


def test_unknown_asset_is_the_assets_error():
    media = make_store("memory", None, duration=60.0, path="x.mp4")
    with pytest.raises(TimelineError, match="a_0000000000000000"):
        highlights.suggest_highlights(media, "a_0000000000000000")


def test_weights_that_zero_every_available_signal_are_an_error(mixed):
    _, _, index = mixed
    zero = {s: 0 for s in highlights.POSITIVE}
    with pytest.raises(highlights.HighlightsError, match="every weight"):
        highlights.suggest_from_index(index, weights=zero)


def test_three_hours_score_well_under_two_seconds(gig, stream):
    for _, _, index in (gig, stream):
        started = time.perf_counter()
        highlights.suggest_from_index(index, preset="default", count=10)
        assert time.perf_counter() - started < 1.0  # about 0.03 s on the development machine
    media, asset, _ = gig
    started = time.perf_counter()
    highlights.suggest_highlights(media, asset.id, preset="gig")  # including loading the stored documents
    assert time.perf_counter() - started < 2.0
