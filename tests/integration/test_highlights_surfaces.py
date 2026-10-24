"""Highlight suggestions through the HTTP API, the CLI and MCP (in memory and over a real stdio subprocess): the same
JSON must come out of every surface (SPL-48). Signals are planted (tests/fake_highlights.py)."""

import json
import sys

import fake_highlights as fh
import pytest
from fake_signals import make_store
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from split_video import highlights
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()


@pytest.fixture
def planted(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    media = make_store("local", root, duration=3600.0, path="mixed.mp4")
    return root, fh.mixed(media, "mixed.mp4", duration=3600.0)


def _cli(root, *args):
    out = runner.invoke(app, ["highlights", *args, "--root", str(root), "--json"])
    assert out.exit_code == 0, out.output
    return out.stdout


async def _call(client, /, **arguments):
    result = await client.call_tool("suggest_highlights", arguments)
    assert not result.is_error, result.content
    return result.structured_content


def test_http_cli_and_engine_agree(planted):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/highlights"
    expected = highlights.suggest_highlights(LocalMediaStore(root), asset.id)
    got = client.get(base)
    assert got.status_code == 200 and got.json() == expected
    assert got.content == highlights.dumps(expected).encode()  # byte for byte what the CLI prints
    assert _cli(root, asset.id).strip() == got.text and _cli(root, "mixed.mp4").strip() == got.text

    params = {
        "preset": "stream",
        "count": 2,
        "min_seconds": 20,
        "max_seconds": 45,
        "start": 500,
        "end": 3000,
        "keywords": "FAILED,error",
        "weights": "speech:3,applause:0.5",
    }
    flags = ["--preset", "stream", "--count", "2", "--min-seconds", "20", "--max-seconds", "45", "--start", "500"]
    flags += ["--end", "0:50:00", "--keywords", "FAILED,error", "--weight", "speech=3", "--weight", "applause=0.5"]
    one = client.get(base, params=params)
    assert one.status_code == 200 and len(one.json()["candidates"]) == 2
    assert one.json()["params"]["weights"]["speech"] == 3.0 and one.json()["params"]["keywords"] == ["FAILED", "error"]
    assert json.loads(_cli(root, asset.id, *flags)) == one.json()
    as_json = client.get(base, params={**params, "weights": json.dumps({"speech": 3, "applause": 0.5})})
    assert as_json.json() == one.json()


def test_http_errors(planted):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/highlights"
    assert client.get("/api/v1/assets/a_0000000000000000/highlights").status_code == 404
    for params in (
        {"preset": "karaoke"},
        {"count": 0},
        {"count": "x"},
        {"weights": "bass:1"},
        {"weights": "speech:-1"},
        {"weights": "speech"},
        {"min_seconds": 30, "max_seconds": 10},
        {"start": 900, "end": 100},
    ):
        found = client.get(base, params=params)
        assert found.status_code == 422 and found.json()["detail"], params
    assert "speech, loudness" in client.get(base, params={"weights": "bass:1"}).json()["detail"]
    openapi = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/assets/{asset_id}/highlights" in openapi


def test_no_signals_is_a_conflict_that_names_what_to_run(tmp_path):
    from split_video import assets

    root = tmp_path / "ws"
    root.mkdir()
    media = make_store("local", root, duration=600.0, path="bare.mp4")
    asset = assets.register_path(media, "bare.mp4")
    client = TestClient(create_app(root, DEFAULTS))
    found = client.get(f"/api/v1/assets/{asset.id}/highlights")
    assert found.status_code == 409
    for word in ("report <target> --prepare", "transcribe", "scenes", "ocr"):
        assert word in found.json()["detail"]
    out = runner.invoke(app, ["highlights", "bare.mp4", "--root", str(root)])
    assert out.exit_code == 1 and "transcribe" in out.output


def test_cli_table_and_errors(planted):
    root, asset = planted
    shown = runner.invoke(app, ["highlights", asset.id, "--root", str(root), "--preset", "gig", "--count", "2"])
    assert shown.exit_code == 0, shown.output
    for word in ("preset gig", "score", "applause 30s", "1000", "words/s"):
        assert word in shown.output, word
    for args in (
        ["nope.mp4"],
        ["a_0000000000000000"],
        [asset.id, "--preset", "karaoke"],
        [asset.id, "--weight", "bass=1"],
        [asset.id, "--weight", "speech=-2"],
        [asset.id, "--weight", "speech"],
        [asset.id, "--count", "0"],
        [asset.id, "--start", "soon"],
    ):
        assert runner.invoke(app, ["highlights", *args, "--root", str(root)]).exit_code != 0, args


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_tool_returns_the_same_json(planted, anyio_backend):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/highlights"
    async with Client(create_server(root)) as mcp:
        assert "suggest_highlights" in {t.name for t in (await mcp.list_tools()).tools}
        assert await _call(mcp, asset=asset.id) == client.get(base).json()
        assert (
            await _call(mcp, asset=asset.id, preset="stream", count=2, keywords=["FAILED"], weights={"speech": 3})
            == client.get(
                base, params={"preset": "stream", "count": 2, "keywords": "FAILED", "weights": "speech:3"}
            ).json()
        )
        for args in ({"preset": "karaoke"}, {"weights": {"bass": 1}}, {"weights": {"speech": -1}}, {"count": 0}):
            assert (await mcp.call_tool("suggest_highlights", {"asset": asset.id, **args})).is_error
        assert (await mcp.call_tool("suggest_highlights", {"asset": "a_0000000000000000"})).is_error


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_a_real_stdio_subprocess(planted, anyio_backend):
    root, asset = planted
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(root)]
    )
    async with Client(params) as mcp:
        found = await _call(mcp, asset=asset.id, preset="stream", count=3)
        assert (await mcp.call_tool("suggest_highlights", {"asset": asset.id, "preset": "nope"})).is_error
    client = TestClient(create_app(root, DEFAULTS))
    http = client.get(f"/api/v1/assets/{asset.id}/highlights", params={"preset": "stream", "count": 3}).json()
    assert http == found == json.loads(_cli(root, asset.id, "--preset", "stream", "--count", "3"))
    assert found["candidates"][0]["start"] == 2000.0 and found["candidates"][0]["end"] == 2040.0
