"""Combined search over the transcript and the on-screen text, and the visual search (SPL-99): a planted moment,
decoys, one source or none, score properties, filters and exactly-once paging on three hours (36 000 words and 360
frames), in memory and on the local store."""

import time

import pytest
from fake_ocr import store_ocr
from fake_transcript import make_document, store_transcript

from split_video import assets, ocr, search, transcribe, transcript_search
from split_video.ffmpeg import MediaInfo
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import InMemoryMediaStore

FAILED = "FAILED tests/unit/test_scenes.py::test_cut_burst_keeps_strongest"
# index of the word that starts at about 1830 s (0.3 s a word plus 1 s after every 12 words)
PLANT_WORD = 4774
DECOY_BUILD_WORDS = [900, 2500, 7000, 20_000, 30_000]  # "build" said elsewhere
SCREENS = [
    (float(t), f"def handler_{t // 30}(event):\n    return process(event, {t})") for t in range(0, 10_800, 30)
]  # 360 frames of code


def screens(extra=()):
    table = dict(SCREENS)
    table[1830.0] = "$ make test\n" + FAILED + "\nE   AssertionError: assert 3 == 2"  # the planted terminal
    table[300.0] = "FAILED tests/unit/test_other.py::test_unrelated"  # a decoy: FAILED, no build
    table[7668.0] = "FAILED tests/unit/test_split.py::test_x"  # shown two seconds after a lone "build" is said
    table[5400.0] = "FAILED tests/unit/test_more.py::test_again - build"  # a decoy: both words, 1 line, far away
    table.update(dict(extra))
    return sorted(table.items())


PLANTED_WORDS = {PLANT_WORD: ["the", "build", "failed", "again"], **{i: ["build"] for i in DECOY_BUILD_WORDS}}


@pytest.fixture(params=["memory", "local"])
def media(request, tmp_path):
    if request.param == "local":
        store = LocalMediaStore(tmp_path)
        (tmp_path / "talk.wav").write_bytes(b"RIFF")
        store.probe = lambda media_id: MediaInfo(duration=11_000.0, width=None, height=None, has_audio=True)
        return store
    return InMemoryMediaStore()


@pytest.fixture
def both(media):
    asset, document = store_transcript(media, planted=PLANTED_WORDS)
    store_ocr(media, asset, screens())
    return media, asset, document


def test_planted_moment_ranks_first_with_both_sources_as_evidence(both):
    media, asset, document = both
    found = search.search_asset(media, asset.id, "build failed")
    first = found["hits"][0]
    said = document["segments"][PLANT_WORD // 12]["words"][PLANT_WORD % 12]["start"]
    assert abs(said - 1830) < 2
    assert first["found_in"][0] == "transcript" and set(first["found_in"]) == {"transcript", "ocr"}
    assert first["time"] == pytest.approx(said + 0.3, abs=0.5)  # the earliest evidence: where to seek
    assert {e["source"] for e in first["evidence"]} == {"transcript", "ocr"}
    by_source = {e["source"]: e for e in first["evidence"]}
    assert by_source["transcript"]["match"] == "phrase" and by_source["transcript"]["text"] == "build failed"
    assert (
        by_source["ocr"]["time"] == 1830.0
        and "FAILED" in by_source["ocr"]["text"]
        and by_source["ocr"]["covers"] == ["failed"]
    )
    assert first["score"] > 0.99 and first["match"] == "phrase"
    assert found["sources"] == {"transcript": "searched", "ocr": "searched"}
    # the decoys come after it: a lone "build", a lone FAILED
    rest = found["hits"][1:]
    assert rest and all(h["score"] < first["score"] for h in rest)
    # a frame with both words but no speech is next; the lone FAILED and the lone "build"s are about half
    assert rest[0]["time"] == 5400.0 and rest[0]["found_in"] == ["ocr"] and 0.8 < rest[0]["score"] < 0.9
    # then said and shown apart as single words within 15 s (about 0.75), then the lone words (about half)
    assert set(rest[1]["found_in"]) == {"transcript", "ocr"} and 0.7 < rest[1]["score"] < 0.8
    assert all(h["score"] < 0.51 for h in rest[2:]) and {h["time"] for h in rest[2:]} >= {300.0}
    assert sum(1 for h in rest if h["found_in"] == ["transcript"]) == len(DECOY_BUILD_WORDS) - 1


def test_a_hit_is_in_the_shared_shape(both):
    media, asset, _ = both
    hit = search.search_asset(media, asset.id, "build failed")["hits"][0]
    assert set(hit) == {
        "source",
        "time",
        "end",
        "score",
        "text",
        "context_before",
        "context_after",
        "match",
        "found_in",
        "evidence",
    }
    for e in hit["evidence"]:
        assert {"source", "time", "end", "score", "text", "context_before", "context_after", "match", "covers"} <= set(
            e
        )
        assert 0 <= e["score"] <= 1 and e["end"] >= e["time"]


def test_a_planted_identifier_finds_the_frame(both):
    media, asset, _ = both
    found = search.search_asset(media, asset.id, "test_cut_burst_keeps_strongest")
    top = found["hits"][0]
    assert top["source"] == "ocr" and top["found_in"] == ["ocr"] and top["time"] == 1830.0
    assert top["text"] == FAILED and top["score"] > 0.9
    assert found["hits"][0]["evidence"][0]["frame"] == 61


def test_visual_search_is_the_ocr_search_with_a_sources_summary(both):
    media, asset, _ = both
    found = search.visual_search(media, asset.id, "FAILED")
    plain = ocr.ocr_search(media, asset.id, "FAILED")
    assert found.pop("sources") == {"ocr": "searched"} and found == plain
    assert {h["time"] for h in found["hits"]} == {300.0, 1830.0, 5400.0, 7668.0}
    assert all(h["source"] == "ocr" for h in found["hits"])


def test_visual_search_without_text_says_how_to_make_it(media):
    asset, _ = store_transcript(media, planted=PLANTED_WORDS)
    with pytest.raises(ocr.OcrNotFoundError, match="ocr_asset"):
        search.visual_search(media, asset.id, "FAILED")


def test_transcript_only_says_so(media):
    asset, _ = store_transcript(media, planted=PLANTED_WORDS)
    found = search.search_asset(media, asset.id, "build failed")
    assert found["sources"]["transcript"] == "searched"
    assert found["sources"]["ocr"].startswith("not run:") and "ocr_asset" in found["sources"]["ocr"]
    assert found["hits"][0]["found_in"] == ["transcript"] and found["hits"][0]["score"] > 0.9


def test_ocr_only_says_so(media):
    asset, _ = store_transcript(media, planted=PLANTED_WORDS)
    media.save_cache(asset.path, transcribe.CACHE_NAME, None)  # no transcript any more
    media.save_cache(asset.path, transcribe.INFO_NAME, None)
    store_ocr(media, asset, screens())
    found = search.search_asset(media, asset.id, "build failed")
    assert (
        found["sources"]["transcript"].startswith("not run:") and "transcribe_asset" in found["sources"]["transcript"]
    )
    assert found["sources"]["ocr"] == "searched"
    assert {h["source"] for h in found["hits"]} == {"ocr"}
    assert found["hits"][0]["time"] == 5400.0  # the frame with both words beats the lone FAILED


def test_neither_is_an_error_naming_both_ways_to_make_data(media):
    asset, _ = store_transcript(media, planted=PLANTED_WORDS)
    media.save_cache(asset.path, transcribe.CACHE_NAME, None)
    media.save_cache(asset.path, transcribe.INFO_NAME, None)
    with pytest.raises(search.SearchNotFoundError) as caught:
        search.search_asset(media, asset.id, "build")
    text = str(caught.value)
    for hint in ("split-video transcribe", "transcribe_asset", "/transcribe", "split-video ocr", "ocr_asset", "/ocr"):
        assert hint in text


def test_sources_filter(both):
    media, asset, _ = both
    only_ocr = search.search_asset(media, asset.id, "build failed", sources="ocr")
    assert only_ocr["sources"] == {"transcript": "not requested", "ocr": "searched"}
    assert {h["source"] for h in only_ocr["hits"]} == {"ocr"}
    only_said = search.search_asset(media, asset.id, "build failed", sources=["transcript"])
    assert {h["source"] for h in only_said["hits"]} == {"transcript"}
    assert search.search_asset(media, asset.id, "build failed", sources=" Transcript , ocr ")["hits"][0]["found_in"]
    # asking for a source that has no data while another has: only that one is requested, so it is an error
    media.save_cache(asset.path, ocr.CACHE_NAME, None)
    media.save_cache(asset.path, ocr.INFO_NAME, None)
    with pytest.raises(search.SearchNotFoundError, match="ocr_asset"):
        search.search_asset(media, asset.id, "build", sources="ocr")
    for bad in ("video", "", ",", []):
        with pytest.raises(search.SearchQueryError, match="sources"):
            search.search_asset(media, asset.id, "build", sources=bad)


def test_start_and_end_keep_hits_in_the_span(both):
    media, asset, _ = both
    late = search.search_asset(media, asset.id, "build failed", start=2000)
    assert late["hits"] and all(h["time"] >= 1800 for h in late["hits"])  # ocr text still up counts
    assert all(abs(h["time"] - 1830) > 60 for h in late["hits"])
    early = search.search_asset(media, asset.id, "build failed", end=1000)
    assert {h["time"] for h in early["hits"]} >= {300.0} and all(h["time"] < 1000 for h in early["hits"])
    assert all(h["found_in"] != ["transcript", "ocr"] for h in early["hits"])
    around = search.search_asset(media, asset.id, "build failed", start=1820, end=1850)
    assert around["hits"][0]["found_in"] == ["transcript", "ocr"]
    assert around["range"] == {"start": 1820.0, "end": 1850.0}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "!!"},
        {"query": "x" * 501},
        {"query": "a", "limit": 0},
        {"query": "a", "limit": 201},
        {"query": "a", "limit": True},
        {"query": "a", "context": 11},
        {"query": "a", "context": -1},
        {"query": "a", "start": -1},
        {"query": "a", "start": 10, "end": 10},
        {"query": "a", "start": float("nan")},
        {"query": "a", "cursor": "nope"},
    ],
)
def test_bad_arguments_are_query_errors(both, kwargs):
    media, asset, _ = both
    with pytest.raises(search.SearchQueryError):
        search.search_asset(media, asset.id, **kwargs)


def test_context_is_words_for_speech_and_lines_for_screens(both):
    media, asset, _ = both
    hit = search.search_asset(media, asset.id, "build failed", context=2)["hits"][0]
    by_source = {e["source"]: e for e in hit["evidence"]}
    assert len(by_source["transcript"]["context_before"].split()) == 2 and by_source["transcript"]["context_after"]
    assert by_source["ocr"]["context_before"] == "$ make test" and "AssertionError" in by_source["ocr"]["context_after"]


def test_scores_are_comparable_in_zero_to_one(both):
    media, asset, _ = both
    for query in ("build failed", "build", "FAILED tests", "alpha bravo", "the build failed again", "handler 12"):
        found = search.search_asset(media, asset.id, query, limit=200)
        scores = [h["score"] for h in found["hits"]]
        assert all(0 < s <= 1 for s in scores), query
        assert scores == sorted(scores, reverse=True), query
        keys = [(-h["score"], h["time"]) for h in found["hits"]]
        assert keys == sorted(keys), query  # ties by time
        for hit in found["hits"]:
            for e in hit["evidence"]:
                assert 0 < e["score"] <= 1
                if e["source"] == "transcript":
                    assert 0.9 <= e["score"] <= 1.0  # an exact match of words Whisper was sure of


def test_score_ladder_phrase_both_beats_phrase_one_beats_split_beats_one_word(both):
    media, asset, _ = both
    found = search.search_asset(media, asset.id, "build failed", limit=200)
    said_and_shown = found["hits"][0]["score"]
    one_word = {h["score"] for h in found["hits"] if len(h["evidence"]) == 1 and h["match"] == "words"}
    assert said_and_shown > 0.99 and max(one_word) < 0.51  # one word of two: about half
    phrase_one = search.search_asset(media, asset.id, "the build failed again", sources="transcript")["hits"][0]
    assert phrase_one["match"] == "phrase" and 0.9 <= phrase_one["score"] < said_and_shown
    # said and shown close together but as separate words: above either alone, below an exact phrase
    split = next(h for h in found["hits"] if set(h["found_in"]) == {"transcript", "ocr"} and h["match"] == "words")
    assert max(one_word) < split["score"] < phrase_one["score"] and split["time"] == pytest.approx(7666, abs=1)


def test_said_in_one_place_and_shown_far_away_stay_separate_moments(both):
    media, asset, _ = both
    found = search.search_asset(media, asset.id, "build", limit=200)
    times = sorted(h["time"] for h in found["hits"])
    assert any(abs(t - 900 * 0.38333) < 3 for t in times)  # the lone mentions
    assert found["hits"][0]["found_in"] == ["transcript", "ocr"] or found["hits"][0]["score"] > 0.9
    assert len(found["hits"]) == len(DECOY_BUILD_WORDS) + 2  # the said "build"s, the planted one, one frame
    assert all(len(h["evidence"]) == 1 for h in found["hits"] if h["time"] not in (5400.0,) and h["score"] < 0.99)


def test_stopwords_are_not_searched_alone(both):
    media, asset, _ = both
    found = search.search_asset(media, asset.id, "the part where the build fails")
    assert found["tokens"] == ["build", "fails"]  # no stemming: "failed" is not "fails"
    assert found["hits"] and all(len(h["evidence"]) >= 1 for h in found["hits"])


def test_cursor_is_tied_to_the_query_filters_and_stored_data(both):
    media, asset, document = both
    page = search.search_asset(media, asset.id, "build failed", limit=2)
    assert page["next_cursor"] and page["count"] == 2
    nxt = search.search_asset(media, asset.id, "build failed", limit=2, cursor=page["next_cursor"])
    assert nxt["hits"][0] != page["hits"][0]
    for other in (
        {"query": "build"},
        {"query": "build failed", "sources": "ocr"},
        {"query": "build failed", "start": 1},
    ):
        with pytest.raises(search.SearchQueryError, match="cursor"):
            search.search_asset(media, asset.id, cursor=page["next_cursor"], limit=2, **other)
    transcribe.save(media, asset, make_document(asset.id, planted=PLANTED_WORDS))  # transcribed again
    with pytest.raises(search.SearchQueryError, match="cursor"):
        search.search_asset(media, asset.id, "build failed", limit=2, cursor=page["next_cursor"])
    ocr_page = ocr.ocr_search(media, asset.id, "handler", limit=1)
    with pytest.raises(search.SearchQueryError, match="cursor"):
        search.search_asset(media, asset.id, "handler", cursor=ocr_page["next_cursor"])


def _all_pages(media, asset, query, limit, **kwargs):
    out, cursor, pages = [], None, 0
    while True:
        page = search.search_asset(media, asset.id, query, limit=limit, cursor=cursor, **kwargs)
        out.extend(page["hits"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            return out, pages
        assert page["count"] == limit


def test_paging_three_hours_yields_every_moment_exactly_once(both):
    media, asset, _ = both
    for query in ("alpha bravo", "handler", "alpha", "build failed"):
        whole, _ = _all_pages(media, asset, query, 200)
        total = search.search_asset(media, asset.id, query, limit=1)["total_matches"]
        assert len(whole) == total
        for size in (7, 50) if total < 400 else (100,):  # a page recomputes the ranking: keep page counts modest
            paged, pages = _all_pages(media, asset, query, size)
            assert paged == whole and pages == -(-total // size), (query, size)
        keys = [(h["source"], h["time"]) for h in whole]
        assert len(set(keys)) == len(keys), query  # every moment once


def test_a_three_hour_search_takes_milliseconds_warm(both):
    media, asset, _ = both
    search.search_asset(media, asset.id, "build failed")  # parse the transcript once
    started = time.perf_counter()
    for _ in range(5):
        search.search_asset(media, asset.id, "build failed")
    per_search = (time.perf_counter() - started) / 5
    assert per_search < 1.0, per_search  # about 0.1 s on the dev machine, almost all of it the transcript scan


def test_unknown_asset(media):
    with pytest.raises(assets.AssetNotFoundError):
        search.search_asset(media, "a_0000000000000000", "x")


def test_existing_engines_still_return_what_they_did(both):
    media, asset, _ = both
    said = transcript_search.transcript_search(media, asset.id, "build failed")
    assert said["total_matches"] == 1 and set(said["hits"][0]) == {
        "time",
        "end",
        "text",
        "context_before",
        "context_after",
        "segment",
        "word",
    }
    seen = ocr.ocr_search(media, asset.id, "test_cut_burst_keeps_strongest")
    assert seen["hits"][0]["time"] == 1830.0 and "probability" not in seen["hits"][0]
