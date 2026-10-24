"""Combined and visual search through the HTTP API, the CLI and MCP (in memory and over a real stdio subprocess), on
a real rendered screen clip (tests/screen_clip.py): the OCR text is read by the real pipeline with the fake engine
(`ClipEngine`) and the transcript is a synthetic one with "the build failed" said at about 14 s, while the terminal
shows `FAILED tests/...::test_cut_burst_keeps_strongest` from 12 s. The same JSON must come out of every surface."""

import json
import shutil
import sys

import pytest
from fake_ocr import ClipEngine
from fake_transcript import store_transcript
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from screen_clip import PLANTED, mono_font, screen_clip
from typer.testing import CliRunner

from split_video import assets, ocr
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.mcp_server import create_server

pytestmark = pytest.mark.skipif(mono_font() is None or shutil.which("ffmpeg") is None, reason="needs ffmpeg and a font")
DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()
SAID_AT = 36  # word index: about 13.8 s


@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory):
    path = tmp_path_factory.mktemp("screen") / "screen.mp4"
    screen_clip(path)
    return path.read_bytes()


@pytest.fixture
def workspace(tmp_path, clip_bytes):
    root = tmp_path / "ws"
    (root / "streams").mkdir(parents=True)
    (root / "streams" / "coding.mp4").write_bytes(clip_bytes)
    return root


def _asset(root):
    media = LocalMediaStore(root.resolve())
    return media, assets.register_path(media, "streams/coding.mp4")


def _with_text(root, *, spoken=True, shown=True):
    media, asset = _asset(root)
    if spoken:
        store_transcript(media, "streams/coding.mp4", words=200, planted={SAID_AT: ["the", "build", "failed", "again"]})
    if shown:
        ocr.recognize(media, asset.id, interval=6, engine=ClipEngine())
    return media, asset


def _cli(root, *args):
    out = runner.invoke(app, [*args, "--root", str(root), "--json"])
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def test_api_combined_search_finds_the_planted_moment(workspace):
    _, asset = _with_text(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    found = client.get(f"{base}/search", params={"q": "build failed"}).json()
    top = found["hits"][0]
    assert found["sources"] == {"transcript": "searched", "ocr": "searched"}
    assert top["found_in"] == ["transcript", "ocr"] and 12.0 <= top["time"] <= 14.5 and top["score"] > 0.99
    assert {e["source"] for e in top["evidence"]} == {"transcript", "ocr"}
    assert any("FAILED" in e["text"] for e in top["evidence"]) and {"segment", "word"} <= set(top["evidence"][0]) | set(
        top["evidence"][1]
    )
    ident = client.get(f"{base}/search", params={"q": PLANTED}).json()
    assert ident["hits"][0]["source"] == "ocr" and ident["hits"][0]["time"] == 12.0
    only = client.get(f"{base}/search", params={"q": "build failed", "sources": "transcript"}).json()
    assert only["sources"]["ocr"] == "not requested" and {h["source"] for h in only["hits"]} == {"transcript"}
    cut = client.get(f"{base}/search", params={"q": "failed", "start": 20, "end": 30}).json()
    assert all(h["time"] >= 12 for h in cut["hits"])
    first = client.get(f"{base}/search", params={"q": "build failed", "limit": 1}).json()
    assert first["count"] == 1 and first["next_cursor"]
    second = client.get(
        f"{base}/search", params={"q": "build failed", "limit": 1, "cursor": first["next_cursor"]}
    ).json()
    assert second["hits"][0] != first["hits"][0]

    visual = client.get(f"{base}/search/visual", params={"q": PLANTED}).json()
    assert visual["sources"] == {"ocr": "searched"} and visual["hits"][0]["source"] == "ocr"
    assert {k: v for k, v in visual.items() if k != "sources"} == client.get(
        f"{base}/ocr/search", params={"q": PLANTED}
    ).json()


def test_api_errors(workspace):
    media, asset = _asset(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    neither = client.get(f"{base}/search", params={"q": "x"})
    assert neither.status_code == 404
    for hint in ("transcribe", "ocr"):
        assert hint in neither.json()["detail"]
    assert client.get(f"{base}/search/visual", params={"q": "x"}).status_code == 404
    assert client.get("/api/v1/assets/a_0000000000000000/search", params={"q": "x"}).status_code == 404
    assert client.get(f"{base}/search").status_code == 422
    _with_text(workspace, spoken=False)
    only_ocr = client.get(f"{base}/search", params={"q": "FAILED"}).json()
    assert only_ocr["sources"]["transcript"].startswith("not run:") and only_ocr["sources"]["ocr"] == "searched"
    for params in (
        {"q": " "},
        {"q": "x", "sources": "video"},
        {"q": "x", "limit": 0},
        {"q": "x", "cursor": "nope"},
        {"q": "x", "start": 5, "end": 5},
    ):
        assert client.get(f"{base}/search", params=params).status_code == 422, params
    assert client.get(f"{base}/search/visual", params={"q": "x", "limit": 201}).status_code == 422
    assert client.get(f"{base}/search", params={"q": "x", "sources": "transcript"}).status_code == 404


def test_cli_search_and_visual_search(workspace):
    _, asset = _with_text(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    http = client.get(f"/api/v1/assets/{asset.id}/search", params={"q": "build failed", "context": 2}).json()
    assert _cli(workspace, "search", asset.id, "build failed", "--context", "2") == http
    assert _cli(workspace, "search", "streams/coding.mp4", "build failed", "--context", "2") == http
    narrowed = client.get(
        f"/api/v1/assets/{asset.id}/search",
        params={"q": "failed", "sources": "ocr", "start": 5, "end": 30, "limit": 3},
    ).json()
    args = ("search", asset.id, "failed", "--sources", "ocr", "--start", "5", "--end", "30", "--limit", "3")
    assert _cli(workspace, *args) == narrowed
    visual = client.get(f"/api/v1/assets/{asset.id}/search/visual", params={"q": PLANTED}).json()
    assert _cli(workspace, "visual-search", asset.id, PLANTED) == visual

    shown = runner.invoke(app, ["search", asset.id, "build failed", "--root", str(workspace)])
    assert shown.exit_code == 0 and "transcript+ocr" in shown.output and "matches" in shown.output
    shown = runner.invoke(app, ["visual-search", asset.id, PLANTED, "--root", str(workspace)])
    assert shown.exit_code == 0 and "1 of 1 matches" in shown.output


def test_cli_errors(workspace):
    media, asset = _asset(workspace)
    neither = runner.invoke(app, ["search", asset.id, "x", "--root", str(workspace)])
    assert neither.exit_code == 1 and "split-video transcribe" in neither.output and "split-video ocr" in neither.output
    assert runner.invoke(app, ["visual-search", asset.id, "x", "--root", str(workspace)]).exit_code == 1
    _with_text(workspace, shown=False)
    partial = runner.invoke(app, ["search", asset.id, "build", "--root", str(workspace)])
    assert partial.exit_code == 0 and "ocr:" in partial.output and "split-video ocr" in partial.output
    for args in (
        ["search", asset.id, " "],
        ["search", asset.id, "x", "--sources", "video"],
        ["search", "nope.mp4", "x"],
    ):
        assert runner.invoke(app, [*args, "--root", str(workspace)]).exit_code != 0, args


async def _call(client, tool, /, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_tools_return_the_same_json(workspace, anyio_backend):
    _, asset = _asset(workspace)
    async with Client(create_server(workspace)) as mcp:
        for tool in ("search_asset", "visual_search"):
            result = await mcp.call_tool(tool, {"asset": asset.id, "query": "x"})
            assert result.is_error
        assert "ocr_asset" in (await mcp.call_tool("search_asset", {"asset": asset.id, "query": "x"})).content[0].text
        assert (
            "transcribe_asset"
            in (await mcp.call_tool("search_asset", {"asset": asset.id, "query": "x"})).content[0].text
        )
        _with_text(workspace)
        client = TestClient(create_app(workspace, DEFAULTS))
        base = f"/api/v1/assets/{asset.id}"
        http = client.get(f"{base}/search", params={"q": "build failed", "context": 2}).json()
        assert await _call(mcp, "search_asset", asset=asset.id, query="build failed", context=2) == http
        assert http["hits"][0]["found_in"] == ["transcript", "ocr"]
        part = {"query": "failed", "sources": "ocr", "start": 5.0, "end": 30.0, "limit": 3}
        want = client.get(f"{base}/search", params={**part, "q": part.pop("query")}).json()
        assert await _call(mcp, "search_asset", asset=asset.id, query="failed", **part) == want
        visual = client.get(f"{base}/search/visual", params={"q": PLANTED}).json()
        assert await _call(mcp, "visual_search", asset=asset.id, query=PLANTED) == visual
        for tool, args in (
            ("search_asset", {"query": ""}),
            ("search_asset", {"query": "x", "sources": "video"}),
            ("search_asset", {"query": "x", "cursor": "nope"}),
            ("search_asset", {"query": "x", "limit": 0}),
            ("visual_search", {"query": ""}),
            ("visual_search", {"query": "x", "limit": 0}),
        ):
            assert (await mcp.call_tool(tool, {"asset": asset.id, **args})).is_error, (tool, args)
        assert (await mcp.call_tool("search_asset", {"asset": "a_0000000000000000", "query": "x"})).is_error


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_a_real_stdio_subprocess(workspace, anyio_backend):
    _, asset = _with_text(workspace)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(workspace)],
    )
    async with Client(params) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert {"search_asset", "visual_search"} <= names
        found = await _call(mcp, "search_asset", asset=asset.id, query="build failed")
        assert found["hits"][0]["found_in"] == ["transcript", "ocr"]
        seen = await _call(mcp, "visual_search", asset=asset.id, query=PLANTED)
        assert seen["hits"][0]["time"] == 12.0 and seen["sources"] == {"ocr": "searched"}
    client = TestClient(create_app(workspace, DEFAULTS))
    assert client.get(f"/api/v1/assets/{asset.id}/search", params={"q": "build failed"}).json() == found
