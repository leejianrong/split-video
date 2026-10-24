"""Bounded, paged MCP results for `analyze_audio` and `get_transcript` (SPL-111), on in-memory and local stores."""

import json

import pytest
from fake_transcript import store_transcript

from split_video import project, transcribe, transcript_search
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache
from split_video.ffmpeg import MediaInfo
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import InMemoryMediaStore

CAP = 64 * 1024  # docs/tool-response-caps.md


def size(doc) -> int:
    return len(json.dumps(doc, indent=2).encode())  # the MCP text content is indented JSON


@pytest.fixture(params=["memory", "local"])
def media(request, tmp_path):
    if request.param == "local":
        store = LocalMediaStore(tmp_path)
        (tmp_path / "talk.wav").write_bytes(b"RIFF")
        store.probe = lambda media_id: MediaInfo(duration=11_000.0, width=None, height=None, has_audio=True)
        return store
    return InMemoryMediaStore()


def analyzed(media, count=5200):
    """A 2.5 h-sized analysis: `count` alternating regions."""
    if isinstance(media, InMemoryMediaStore):
        media.add("a.mp4", b"video", info=MediaInfo(duration=9000.0, width=64, height=36, has_audio=True))
    else:
        (media.root / "a.mp4").write_bytes(b"video")
    names = ["music", "speech", "silence_other", "applause_crowd"]
    scores = [
        {b: 0.1 for b in classify.DEFAULT_THRESHOLDS} | {names[(i // 2) % 4]: 0.9} for i in range(count)
    ]  # the dominant bucket changes every 2 frames
    regions = classify.rethreshold(scores, classify.DEFAULT_THRESHOLDS, count * 0.48)
    ClassificationCache(media, "a.mp4").store_result(scores, count * 0.48, regions=regions)
    return ClassificationCache(media, "a.mp4")


def test_analysis_pages_concatenate_to_the_full_regions_and_stay_under_the_cap(media):
    cache = analyzed(media)
    full = project.summarize_classification(cache)
    assert size(full) > CAP  # the unbounded shape really was too big
    pages, cursor = [], None
    while True:
        page = project.page_classification(cache, cursor=cursor)
        assert size(page) < CAP
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [r for p in pages for r in p["regions"]] == full["regions"]
    first = pages[0]
    assert first["truncated"] and first["notices"] and first["count"] == project.REGION_PAGE_DEFAULT
    assert first["seconds_by_bucket"] == full["seconds_by_bucket"] and first["analyzed"] is True
    assert first["total_regions"] == len(full["regions"]) == sum(first["regions_by_bucket"].values())
    assert pages[-1]["truncated"] and pages[-1]["notices"] == []  # a later page is still "not the first"
    biggest = project.page_classification(cache, limit=project.REGION_PAGE_MAX)
    assert size(biggest) < CAP


def test_small_analysis_is_untruncated_and_bad_arguments_are_rejected(media):
    cache = analyzed(media, count=10)
    page = project.page_classification(cache)
    assert (
        page["truncated"] is False
        and page["next_cursor"] is None
        and page["count"] == len(project.summarize_classification(cache)["regions"])
    )
    for bad in (0, project.REGION_PAGE_MAX + 1, True):
        with pytest.raises(ValueError, match="limit"):
            project.page_classification(cache, limit=bad)
    with pytest.raises(ValueError, match="cursor"):
        project.page_classification(cache, cursor="nonsense")
    big = analyzed(media, count=400)
    token = project.page_classification(big, limit=10)["next_cursor"]
    with pytest.raises(ValueError, match="made again"):
        project.page_classification(cache, cursor=token)


def test_transcript_pages_concatenate_to_the_excerpt_and_stay_under_the_cap(media):
    asset, document = store_transcript(media)
    full = transcribe.excerpt(document)
    assert size(full) > CAP
    pages, cursor = [], None
    while True:
        page = transcript_search.transcript_page(media, asset.id, cursor=cursor)
        assert size(page) < CAP
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [s for p in pages for s in p["segments"]] == full["segments"]
    first = pages[0]
    assert first["truncated"] and first["notices"] and len(pages) > 1
    assert first["total_segments"] == len(full["segments"])
    assert first["total_words"] == sum(len(s["words"]) for s in full["segments"])
    assert first["first_start"] == full["segments"][0]["start"] and first["last_end"] == full["segments"][-1]["end"]
    assert {k: v for k, v in first.items() if k in ("model", "language", "duration")} == {
        k: full[k] for k in ("model", "language", "duration")
    }


def test_transcript_range_limit_and_small_results(media):
    asset, document = store_transcript(media)
    cut = transcript_search.transcript_page(media, asset.id, 100.0, 160.0)
    expected = transcribe.excerpt(document, 100.0, 160.0)
    assert cut["segments"] == expected["segments"] and cut["truncated"] is False and cut["next_cursor"] is None
    pages, cursor = [], None
    while True:
        page = transcript_search.transcript_page(media, asset.id, 100.0, 5000.0, limit=7, cursor=cursor)
        assert page["count"] <= 7
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [s for p in pages for s in p["segments"]] == transcribe.excerpt(document, 100.0, 5000.0)["segments"]
    empty = transcript_search.transcript_page(media, asset.id, 1e7, 1e7 + 1)
    assert empty["segments"] == [] and empty["first_start"] is None and empty["truncated"] is False
    for kwargs in ({"limit": 0}, {"limit": 501}, {"cursor": "bogus"}):
        with pytest.raises(transcript_search.TranscriptQueryError):
            transcript_search.transcript_page(media, asset.id, **kwargs)


def test_transcript_page_without_a_transcript_is_the_missing_error(media):
    asset, _ = store_transcript(media)
    media.delete_cache(asset.id, "transcript") if hasattr(media, "delete_cache") else None
    with pytest.raises(Exception) as unknown:
        transcript_search.transcript_page(media, "a_0000000000000000")
    assert not isinstance(unknown.value, transcript_search.TranscriptQueryError)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_analyze_audio_returns_a_bounded_page_and_pages_through_it(tmp_path, anyio_backend):
    from mcp import Client

    from split_video.mcp_server import create_server

    root = tmp_path.resolve()
    store = LocalMediaStore(root)
    cache = analyzed(store)
    full = project.summarize_classification(cache)["regions"]
    async with Client(create_server(root)) as mcp:
        regions, cursor, sizes = [], None, []
        while True:
            args = {"video": "a.mp4"} | ({"cursor": cursor} if cursor else {})
            res = await mcp.call_tool("analyze_audio", args)
            assert not res.is_error
            sizes.append(sum(len(c.text.encode()) for c in res.content))
            page = res.structured_content
            regions += page["regions"]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert regions == full and len(sizes) > 1 and max(sizes) < CAP
        assert page["video"] == "a.mp4" and page["total_regions"] == len(full)
        bad = await mcp.call_tool("analyze_audio", {"video": "a.mp4", "limit": 0})
        assert bad.is_error and "request.invalid" in bad.content[0].text
