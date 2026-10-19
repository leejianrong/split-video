"""Paged range reads and phrase search over stored transcripts (in-memory and local store, synthetic transcripts)."""

import time

import pytest
from fake_transcript import make_document, store_transcript

from split_video import transcribe, transcript_search
from split_video.ffmpeg import MediaInfo
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import InMemoryMediaStore
from split_video.transcript_search import TranscriptNotFoundError, TranscriptQueryError

PLANTED = {
    100: ["the", "Quick,", "brown", "fox"],
    # word 1019 ("phrase") is the last of segment 84 (12 words per segment): the phrase runs into segment 85
    1018: ["seam", "phrase", "crosses", "over"],
    2000: ["Don't", "stop—believing"],
    3000: ["well-known", "fact."],
    35_990: ["the", "very", "end"],
}


@pytest.fixture(params=["memory", "local"])
def stored(request, tmp_path):
    if request.param == "local":
        media = LocalMediaStore(tmp_path)
        (tmp_path / "talk.wav").write_bytes(b"RIFF")  # the store only fingerprints it; probing is bypassed below
        media.probe = lambda media_id: MediaInfo(duration=11_000.0, width=None, height=None, has_audio=True)
    else:
        media = InMemoryMediaStore()
    asset, document = store_transcript(media, planted=PLANTED)
    return media, asset, document


def page_all(call, key, **kwargs):
    pages, cursor = [], None
    while True:
        page = call(cursor=cursor, **kwargs)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 100_000


# --- range -----------------------------------------------------------------------------------


def test_paging_a_three_hour_transcript_yields_every_segment_exactly_once(stored):
    media, asset, document = stored
    pages = page_all(
        lambda cursor: transcript_search.transcript_range(media, asset.id, limit=7, cursor=cursor), "items"
    )
    indexes = [i["index"] for p in pages for i in p["items"]]
    assert indexes == list(range(len(document["segments"])))  # in order, no duplicates, no gaps
    assert all(p["count"] <= 7 for p in pages) and pages[-1]["next_cursor"] is None
    assert {p["total_in_range"] for p in pages} == {len(document["segments"])}
    assert pages[0]["items"][0]["text"] == document["segments"][0]["text"]
    assert document["duration"] > 10_800


def test_paging_words_and_with_a_char_budget_is_also_exactly_once(stored):
    media, asset, document = stored
    words = [w for s in document["segments"] for w in s["words"]]
    pages = page_all(
        lambda cursor: transcript_search.transcript_range(
            media, asset.id, 500, 700, granularity="word", limit=13, cursor=cursor
        ),
        "items",
    )
    got = [i for p in pages for i in p["items"]]
    expected = [i for i, w in enumerate(words) if w["end"] > 500 and w["start"] < 700]
    assert [i["index"] for i in got] == expected and len(set(expected)) == pages[0]["total_in_range"]
    assert got[0]["segment"] == got[0]["index"] // 12 and got[0]["word"] == words[got[0]["index"]]["word"]

    budget = page_all(
        lambda cursor: transcript_search.transcript_range(media, asset.id, 0, 400, max_chars=300, cursor=cursor),
        "items",
    )
    assert all(sum(len(i["text"]) for i in p["items"]) <= 300 or p["count"] == 1 for p in budget)
    assert len(budget) > 1
    flat = [i["index"] for p in budget for i in p["items"]]
    assert flat == list(range(len(flat))) and len(flat) == budget[0]["total_in_range"]


def test_a_range_includes_overlapping_segments_and_excludes_touching_ones(stored):
    media, asset, document = stored
    seg = document["segments"]
    # Starts inside segment 3: segment 3 started earlier but is still running, so it is in.
    inside = (seg[3]["start"] + seg[3]["end"]) / 2
    page = transcript_search.transcript_range(media, asset.id, inside, seg[5]["start"])
    assert [i["index"] for i in page["items"]] == [3, 4]  # segment 5 starts exactly at `end`: not included
    assert page["items"][0]["start"] < inside  # whole segment, not cut
    # `start` exactly at a segment's end: that segment ended, it is out.
    page = transcript_search.transcript_range(media, asset.id, seg[3]["end"], seg[5]["start"])
    assert [i["index"] for i in page["items"]] == [4]
    assert page["covered"] == {"start": seg[4]["start"], "end": seg[4]["end"]}
    assert page["range"] == {"start": seg[3]["end"], "end": seg[5]["start"]}
    open_end = transcript_search.transcript_range(media, asset.id, seg[-2]["start"])
    assert [i["index"] for i in open_end["items"]] == [len(seg) - 2, len(seg) - 1] and open_end["range"]["end"] is None


def test_empty_ranges_and_defaults(stored):
    media, asset, document = stored
    far = transcript_search.transcript_range(media, asset.id, 50_000, 60_000)
    assert far["items"] == [] and far["covered"] is None and far["total_in_range"] == 0 and far["next_cursor"] is None
    first = transcript_search.transcript_range(media, asset.id)
    assert first["count"] == transcript_search.DEFAULT_SEGMENT_LIMIT and first["next_cursor"]
    assert transcript_search.transcript_range(media, asset.id, granularity="word")["count"] == 200
    assert transcript_search.transcript_range(media, asset.id, limit=500)["count"] == 500


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"start": -1}, "negative"),
        ({"start": 5, "end": 5}, "after start"),
        ({"start": 5, "end": 2}, "after start"),
        ({"start": float("nan")}, "number"),
        ({"end": float("inf")}, None),
        ({"limit": 0}, "limit"),
        ({"limit": 501}, "limit"),
        ({"limit": True}, "limit"),
        ({"max_chars": 0}, "max_chars"),
        ({"granularity": "paragraph"}, "granularity"),
        ({"cursor": "garbage"}, "cursor"),
    ],
)
def test_range_validation(stored, kwargs, message):
    media, asset, _ = stored
    if message is None:
        assert transcript_search.transcript_range(media, asset.id, **kwargs)
        return
    with pytest.raises(TranscriptQueryError, match=message):
        transcript_search.transcript_range(media, asset.id, **kwargs)


def test_a_cursor_is_for_its_kind_and_its_transcript(stored):
    media, asset, document = stored
    seg_cursor = transcript_search.transcript_range(media, asset.id, limit=5)["next_cursor"]
    with pytest.raises(TranscriptQueryError, match="different kind"):
        transcript_search.transcript_range(media, asset.id, granularity="word", cursor=seg_cursor)
    with pytest.raises(TranscriptQueryError, match="different kind"):
        transcript_search.transcript_search(media, asset.id, "alpha", cursor=seg_cursor)
    transcribe.save(media, transcribe.assets.get_asset(media, asset.id), make_document(asset.id, words=600))
    with pytest.raises(TranscriptQueryError, match="made again"):
        transcript_search.transcript_range(media, asset.id, limit=5, cursor=seg_cursor)


# --- search ----------------------------------------------------------------------------------


def test_search_finds_a_phrase_with_time_and_context(stored):
    media, asset, document = stored
    words = [w for s in document["segments"] for w in s["words"]]
    found = transcript_search.transcript_search(media, asset.id, "quick BROWN", context=3)
    (hit,) = found["hits"]
    assert found["total_matches"] == 1 and found["next_cursor"] is None
    assert hit["time"] == words[101]["start"] and hit["end"] == words[102]["end"]
    assert hit["text"] == "Quick, brown"  # as stored
    assert hit["context_before"].endswith("the") and hit["context_after"].startswith("fox")
    assert len(hit["context_before"].split()) == 3 and len(hit["context_after"].split()) == 3
    assert hit["word"] == 101 and hit["segment"] == 101 // 12
    assert (
        found["hits"][0]["segment"]
        == transcript_search.transcript_range(media, asset.id, hit["time"], hit["time"] + 0.01)["items"][0]["index"]
    )


def test_a_phrase_can_span_two_segments(stored):
    media, asset, document = stored
    (hit,) = transcript_search.transcript_search(media, asset.id, "phrase crosses over")["hits"]
    assert hit["word"] == 1019 and hit["segment"] == 84
    assert document["segments"][84]["words"][-1]["word"] == "phrase"  # starts in 84, finishes in 85
    assert hit["text"] == "phrase crosses over"
    assert hit["end"] > document["segments"][85]["start"]


def test_normalisation_of_case_punctuation_and_hyphens(stored):
    media, asset, _ = stored

    def times(query, **kw):
        return [h["word"] for h in transcript_search.transcript_search(media, asset.id, query, **kw)["hits"]]

    assert times("DON'T stop") == [2000] and times("don’t stop") == [2000]
    assert times("stop believing") == [2001]  # an em dash breaks a word
    assert times("well known fact") == [3000] and times("well-known fact") == [3000]
    assert times("  the   quick  ") == [100] and times("!!quick??") == [101]
    assert times("quick", case_sensitive=True) == [] and times("Quick", case_sensitive=True) == [101]
    assert times("Don't", case_sensitive=True) == [2000]
    assert times("quic") == [] and times("rown fox") == []  # whole words only


def test_no_match_and_all_matches_page_in_time_order(stored):
    media, asset, document = stored
    none = transcript_search.transcript_search(media, asset.id, "zebra")
    assert none["hits"] == [] and none["total_matches"] == 0 and none["next_cursor"] is None
    pages = page_all(
        lambda cursor: transcript_search.transcript_search(media, asset.id, "alpha", limit=40, cursor=cursor), "hits"
    )
    hits = [h for p in pages for h in p["hits"]]
    words = [w for s in document["segments"] for w in s["words"]]
    assert [h["word"] for h in hits] == [i for i, w in enumerate(words) if w["word"] == "alpha"]
    assert [h["time"] for h in hits] == sorted(h["time"] for h in hits)
    assert len({h["word"] for h in hits}) == len(hits) == pages[0]["total_matches"] and len(pages) > 3


def test_search_range_restricts_hits_by_their_first_word(stored):
    media, asset, document = stored
    words = [w for s in document["segments"] for w in s["words"]]
    everywhere = transcript_search.transcript_search(media, asset.id, "the very end")
    assert [h["word"] for h in everywhere["hits"]] == [35_990]
    early = transcript_search.transcript_search(media, asset.id, "the very end", end=1000)
    assert early["hits"] == []
    late = transcript_search.transcript_search(media, asset.id, "the very end", start=words[35_990]["start"])
    assert [h["word"] for h in late["hits"]] == [35_990]
    assert late["hits"][0]["context_after"] == " ".join(w["word"] for w in words[35_993 : 35_993 + 8])  # to the end
    after = transcript_search.transcript_search(media, asset.id, "the very end", start=words[35_990]["end"] + 0.001)
    assert after["hits"] == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": ""}, "empty"),
        ({"query": "   "}, "empty"),
        ({"query": "?! --"}, "empty"),
        ({"query": "x" * 501}, "500"),
        ({"query": "a", "limit": 0}, "limit"),
        ({"query": "a", "limit": 201}, "limit"),
        ({"query": "a", "context": -1}, "context"),
        ({"query": "a", "context": 51}, "context"),
        ({"query": "a", "start": 9, "end": 3}, "after start"),
        ({"query": "a", "cursor": "!!"}, "cursor"),
    ],
)
def test_search_validation(stored, kwargs, message):
    media, asset, _ = stored
    with pytest.raises(TranscriptQueryError, match=message):
        transcript_search.transcript_search(media, asset.id, **kwargs)


def test_errors_for_no_transcript_and_unknown_asset():
    media = InMemoryMediaStore()
    media.add("talk.wav", b"x", info=MediaInfo(duration=5.0, width=None, height=None, has_audio=True))
    asset = transcribe.assets.register_path(media, "talk.wav")
    for call in (
        lambda: transcript_search.transcript_range(media, asset.id),
        lambda: transcript_search.transcript_search(media, asset.id, "x"),
    ):
        with pytest.raises(TranscriptNotFoundError, match=r"split-video transcribe.*transcribe_asset.*/transcribe"):
            call()
    with pytest.raises(Exception, match="unknown asset"):
        transcript_search.transcript_range(media, "a_0000000000000000")
    with pytest.raises(Exception, match="unknown asset"):
        transcript_search.transcript_search(media, "a_0000000000000000", "x")


def test_a_silent_recording_has_empty_pages():
    media = InMemoryMediaStore()
    asset, _ = store_transcript(media, words=0)
    empty = transcript_search.transcript_range(media, asset.id)
    assert empty["items"] == [] and empty["total_in_range"] == 0 and empty["next_cursor"] is None
    assert transcript_search.transcript_search(media, asset.id, "x")["hits"] == []


def test_search_of_a_three_hour_transcript_takes_milliseconds(stored):
    media, asset, _ = stored
    transcript_search.transcript_search(media, asset.id, "alpha")  # warm
    started = time.perf_counter()
    transcript_search.transcript_search(media, asset.id, "the very end")
    assert time.perf_counter() - started < 1.0  # loose: CI machines are slow; real numbers are in the PR


def test_a_transcript_made_again_is_never_served_from_a_stale_copy(stored):
    media, asset, _ = stored
    assert transcript_search.transcript_search(media, asset.id, "zulu yankee")["hits"] == []
    again = make_document(asset.id, words=600, planted={5: ["zulu", "yankee"]})
    transcribe.save(media, transcribe.assets.get_asset(media, asset.id), again)
    (hit,) = transcript_search.transcript_search(media, asset.id, "zulu yankee")["hits"]
    assert hit["word"] == 5
    assert transcript_search.transcript_range(media, asset.id)["total_in_range"] == 50
