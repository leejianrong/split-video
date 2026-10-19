"""Transcript range and search through the HTTP API, the CLI and MCP (in memory and over a real stdio subprocess):
the same JSON shape on all three, paging, and the old whole-transcript shapes still working."""

import json
import sys

import pytest
from fake_transcript import make_document
from fake_whisper import speech, write_wav
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from split_video import assets, transcribe, transcript_search
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()
PLANTED = {20: ["the", "magic", "word"], 35: ["seam", "phrase", "here"], 400: ["magic", "word", "again"]}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    pcm, _ = speech([0.5], 3.0)
    write_wav(root / "talk.wav", pcm)
    other, _ = speech([1.5], 3.0)  # different bytes, so a different asset
    write_wav(root / "bare.wav", other)  # never transcribed
    media = LocalMediaStore(root.resolve())
    asset = assets.register_path(media, "talk.wav")
    transcribe.save(media, asset, make_document(asset.id, words=600, per_segment=10, planted=PLANTED))
    return root, asset.id, assets.register_path(media, "bare.wav").id


def _cli(root, *args):
    return runner.invoke(app, [*args, "--root", str(root)])


def test_http_page_and_search_and_the_old_shape(workspace):
    root, asset, bare = workspace
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset}/transcript"
    whole = client.get(base).json()  # no paging arguments: the whole transcript, as before
    assert len(whole["segments"]) == 60 and whole["segments"][0]["words"][0]["word"] and "items" not in whole
    assert [len(s["words"]) for s in client.get(base, params={"start": 2, "end": 4}).json()["segments"]] == [4]

    page = client.get(base, params={"limit": 25, "start": 10}).json()
    assert page["count"] == 25 and page["granularity"] == "segment" and page["next_cursor"]
    assert page["items"][0].keys() == {"index", "start", "end", "text"} and "segments" not in page
    second = client.get(base, params={"limit": 25, "start": 10, "cursor": page["next_cursor"]}).json()
    assert second["items"][0]["index"] == page["items"][-1]["index"] + 1
    words = client.get(base, params={"granularity": "word", "limit": 4}).json()
    assert [i["index"] for i in words["items"]] == [0, 1, 2, 3] and "word" in words["items"][0]
    assert client.get(base, params={"max_chars": 100}).json()["count"] < 50

    found = client.get(f"{base}/search", params={"q": "Magic word", "context": 2}).json()
    assert [h["word"] for h in found["hits"]] == [21, 400] and found["total_matches"] == 2
    assert found["hits"][0]["context_before"].endswith("the") and found["hits"][0]["text"] == "magic word"
    one = client.get(f"{base}/search", params={"q": "magic word", "limit": 1}).json()
    nxt = client.get(f"{base}/search", params={"q": "magic word", "limit": 1, "cursor": one["next_cursor"]}).json()
    assert [one["hits"][0]["word"], nxt["hits"][0]["word"]] == [21, 400] and nxt["next_cursor"] is None
    assert [h["word"] for h in client.get(f"{base}/search", params={"q": "phrase here seam"}).json()["hits"]] == []
    assert len(client.get(f"{base}/search", params={"q": "seam phrase"}).json()["hits"]) == 1


def test_http_errors(workspace):
    root, asset, bare = workspace
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset}/transcript"
    for params in ({"limit": 0}, {"limit": 501}, {"limit": 5, "cursor": "nope"}, {"granularity": "para"}):
        assert client.get(base, params=params).status_code == 422, params
    assert client.get(base, params={"limit": 5, "start": 9, "end": 3}).status_code == 422
    for params in ({"q": ""}, {"q": "   "}, {"q": "x", "limit": 201}, {"q": "x" * 501}, {"q": "x", "start": -1}):
        assert client.get(f"{base}/search", params=params).status_code == 422, params
    assert client.get(f"{base}/search").status_code == 422  # q is required
    missing = client.get(f"/api/v1/assets/{bare}/transcript/search", params={"q": "x"})
    assert missing.status_code == 404 and "transcribe" in missing.json()["detail"]
    assert client.get(f"/api/v1/assets/{bare}/transcript", params={"limit": 5}).status_code == 404
    assert client.get(f"/api/v1/assets/{bare}/transcript").status_code == 404
    assert client.get("/api/v1/assets/a_0000000000000000/transcript/search", params={"q": "x"}).status_code == 404


def test_cli_matches_http_and_keeps_the_old_command(workspace):
    root, asset, bare = workspace
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset}/transcript"

    old = json.loads(_cli(root, "transcript", asset, "--json").stdout)
    assert old == client.get(base).json()
    cli_page = json.loads(_cli(root, "transcript", "talk.wav", "--limit", "7", "--start", "5", "--json").stdout)
    assert cli_page == client.get(base, params={"limit": 7, "start": 5}).json()
    cli_next = json.loads(
        _cli(
            root, "transcript", asset, "--limit", "7", "--start", "5", "--cursor", cli_page["next_cursor"], "--json"
        ).stdout
    )
    assert cli_next["items"][0]["index"] == cli_page["items"][-1]["index"] + 1
    cli_words = json.loads(_cli(root, "transcript", asset, "--words", "--limit", "3", "--json").stdout)
    assert cli_words == client.get(base, params={"granularity": "word", "limit": 3}).json()
    cli_hits = json.loads(_cli(root, "transcript-search", asset, "magic word", "--context", "3", "--json").stdout)
    assert cli_hits == client.get(f"{base}/search", params={"q": "magic word", "context": 3}).json()

    plain = _cli(root, "transcript", asset, "--limit", "2")
    assert plain.exit_code == 0 and "More: add --cursor" in plain.output and "2 of 60 in range" in plain.output
    text = _cli(root, "transcript-search", asset, "magic word")
    assert text.exit_code == 0 and "magic word" in text.output and "2 of 2 matches" in text.output


def test_cli_errors(workspace):
    root, asset, bare = workspace
    for args in (
        ["transcript", asset, "--limit", "0"],
        ["transcript", asset, "--limit", "5", "--start", "4", "--end", "1"],
        ["transcript", asset, "--cursor", "junk"],
        ["transcript-search", asset, "  "],
        ["transcript-search", asset, "x", "--limit", "999"],
        ["transcript-search", bare, "x"],
        ["transcript", bare, "--limit", "3"],
        ["transcript", bare],
        ["transcript-search", "a_0000000000000000", "x"],
    ):
        out = _cli(root, *args)
        assert out.exit_code == 1, args
    out = _cli(root, "transcript-search", bare, "x")
    assert "split-video transcribe" in " ".join(out.output.split())


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_matches_http(workspace, anyio_backend):
    root, asset, bare = workspace
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset}/transcript"
    async with Client(create_server(root)) as mcp:
        page = (await mcp.call_tool("transcript_range", {"asset": asset, "limit": 9, "start": 3})).structured_content
        assert page == client.get(base, params={"limit": 9, "start": 3}).json()
        after = (
            await mcp.call_tool(
                "transcript_range", {"asset": asset, "limit": 9, "start": 3, "cursor": page["next_cursor"]}
            )
        ).structured_content
        assert after["items"][0]["index"] == page["items"][-1]["index"] + 1
        words = (
            await mcp.call_tool("transcript_range", {"asset": asset, "granularity": "word", "limit": 5})
        ).structured_content
        assert words == client.get(base, params={"granularity": "word", "limit": 5}).json()
        hits = (await mcp.call_tool("transcript_search", {"asset": asset, "query": "magic word"})).structured_content
        assert hits == client.get(f"{base}/search", params={"q": "magic word"}).json()
        # get_transcript keeps working, unchanged
        whole = (await mcp.call_tool("get_transcript", {"asset": asset})).structured_content
        assert whole == client.get(base).json()
        for tool, args in (
            ("transcript_range", {"asset": asset, "limit": 0}),
            ("transcript_range", {"asset": asset, "granularity": "x"}),
            ("transcript_range", {"asset": bare}),
            ("transcript_search", {"asset": asset, "query": ""}),
            ("transcript_search", {"asset": bare, "query": "x"}),
            ("transcript_search", {"asset": "a_0000000000000000", "query": "x"}),
        ):
            assert (await mcp.call_tool(tool, args)).is_error, (tool, args)
        text = (await mcp.call_tool("transcript_search", {"asset": bare, "query": "x"})).content[0].text
        assert "transcribe_asset" in text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_a_real_stdio_subprocess(workspace, anyio_backend):
    root, asset, _ = workspace
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(root)]
    )
    async with Client(params) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert {"transcript_range", "transcript_search", "get_transcript"} <= names
        seen, cursor = [], None
        while True:
            args = {"asset": asset, "limit": 11} | ({"cursor": cursor} if cursor else {})
            page = (await mcp.call_tool("transcript_range", args)).structured_content
            seen += [i["index"] for i in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert seen == list(range(60))
        hit = (
            await mcp.call_tool("transcript_search", {"asset": asset, "query": "seam phrase here"})
        ).structured_content
        assert hit["total_matches"] == 1 and hit["hits"][0]["time"] > 0
        assert transcript_search.MAX_SEARCH_LIMIT == 200
