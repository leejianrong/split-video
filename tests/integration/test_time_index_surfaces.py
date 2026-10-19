"""What is happening at a time, through the HTTP API, the CLI and MCP (in memory and over a real stdio subprocess),
on planted signals (tests/fake_signals.py): the same JSON must come out of every surface (SPL-100)."""

import json
import sys

import pytest
from fake_signals import DURATION, make_store, plant
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from split_video import time_index
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    media = make_store("local", root, path="talk.mp4")
    return root, media


@pytest.fixture
def planted(workspace):
    root, media = workspace
    return root, plant(media)


def _cli(root, *args):
    out = runner.invoke(app, [*args, "--root", str(root), "--json"])
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


async def _call(client, tool, /, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


def test_http_at_and_timeline(planted):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    found = client.get(f"{base}/at", params={"time": 205}).json()
    assert found["audio"]["class"] == "applause_crowd" and found["loudness"]["max_db"] == -3.0
    assert found["window"] == {"start": 200.0, "end": 210.0} and found["missing"] == []
    assert "class" in found["audio"] and found["visual"]["description"] is None
    media = LocalMediaStore(root)
    assert found == time_index.what_is_happening(media, asset.id, 205.0)
    wide = client.get(f"{base}/at", params={"time": 100, "window": 2}).json()
    assert wide["window"] == {"start": 99.0, "end": 101.0} and wide["speech"]["word_count"] == 4
    assert wide == time_index.what_is_happening(media, asset.id, 100, 2)
    line = client.get(f"{base}/timeline", params={"start": 0, "end": 120, "step": 30}).json()
    assert line == time_index.timeline_index(media, asset.id, 0, 120, 30) and line["count"] == 4
    assert client.get(f"{base}/timeline").json()["count"] == 5


def test_http_missing_signals_are_data_not_errors(workspace):
    root, media = workspace
    asset = plant(media, skip=("speech", "ocr", "audio"))
    client = TestClient(create_app(root, DEFAULTS))
    found = client.get(f"/api/v1/assets/{asset.id}/at", params={"time": 100})
    assert found.status_code == 200
    body = found.json()
    assert body["speech"] is None and body["audio"] is None and body["visual"]["text"] is None
    assert [m["signal"] for m in body["missing"]] == ["audio", "speech", "ocr"]
    assert body["loudness"] is not None and body["visual"]["scene_index"] == 1


def test_http_errors(planted):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    assert client.get("/api/v1/assets/a_0000000000000000/at", params={"time": 1}).status_code == 404
    assert client.get("/api/v1/assets/a_0000000000000000/timeline").status_code == 404
    assert client.get(f"{base}/at").status_code == 422  # no time
    assert client.get(f"{base}/at", params={"time": "soon"}).status_code == 422
    for params in (
        {"time": -1},
        {"time": DURATION + 1},
        {"time": 10, "window": 0},
        {"time": 10, "window": 601},
        {"time": 10, "window": -3},
    ):
        found = client.get(f"{base}/at", params=params)
        assert found.status_code == 422 and found.json()["detail"], params
    for params in ({"start": -1}, {"start": 10, "end": 10}, {"end": 999}, {"step": 0.5}):
        assert client.get(f"{base}/timeline", params=params).status_code == 422, params


def test_cli_matches_http(planted):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    assert _cli(root, "at", asset.id, "205") == client.get(f"{base}/at", params={"time": 205}).json()
    assert _cli(root, "at", "talk.mp4", "205") == client.get(f"{base}/at", params={"time": 205}).json()
    assert (
        _cli(root, "at", asset.id, "1:40", "--window", "2")
        == client.get(f"{base}/at", params={"time": 100, "window": 2}).json()
    )
    line = client.get(f"{base}/timeline", params={"start": 60, "end": 180, "step": 20}).json()
    assert _cli(root, "timeline-index", asset.id, "--start", "1:00", "--end", "3:00", "--step", "20") == line
    assert _cli(root, "timeline-index", asset.id) == client.get(f"{base}/timeline").json()


def test_cli_text_output_and_errors(workspace):
    root, media = workspace
    asset = plant(media, skip=("speech",))
    shown = runner.invoke(app, ["at", asset.id, "205", "--root", str(root)])
    assert shown.exit_code == 0, shown.output
    assert "applause_crowd" in shown.output and "loudness" in shown.output and "split-video transcribe" in shown.output
    table = runner.invoke(app, ["timeline-index", asset.id, "--root", str(root)])
    assert table.exit_code == 0 and "split-video transcribe" in table.output
    for args in (
        ["at", asset.id, "-5"],
        ["at", asset.id, "9999"],
        ["at", asset.id, "soon"],
        ["at", asset.id, "10", "--window", "0"],
        ["at", asset.id, "10", "--window", "601"],
        ["at", "nope.mp4", "10"],
        ["at", "a_0000000000000000", "10"],
        ["timeline-index", asset.id, "--step", "0.5"],
        ["timeline-index", asset.id, "--start", "100", "--end", "50"],
        ["timeline-index", "nope.mp4"],
    ):
        assert runner.invoke(app, [*args, "--root", str(root)]).exit_code != 0, args
    # a negative time is read as an option by the shell parser; after `--` it is a time and refused
    refused = runner.invoke(app, ["at", "--root", str(root), "--", asset.id, "-5"])
    assert refused.exit_code != 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_tools_return_the_same_json(planted, anyio_backend):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    async with Client(create_server(root)) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert {"what_is_happening", "timeline_index"} <= names
        assert (
            await _call(mcp, "what_is_happening", asset=asset.id, time=205)
            == client.get(f"{base}/at", params={"time": 205}).json()
        )
        assert (
            await _call(mcp, "what_is_happening", asset=asset.id, time=100, window=2)
            == client.get(f"{base}/at", params={"time": 100, "window": 2}).json()
        )
        assert (
            await _call(mcp, "timeline_index", asset=asset.id, start=0, end=120, step=30)
            == client.get(f"{base}/timeline", params={"start": 0, "end": 120, "step": 30}).json()
        )
        assert await _call(mcp, "timeline_index", asset=asset.id) == client.get(f"{base}/timeline").json()
        for tool, args in (
            ("what_is_happening", {"time": -1}),
            ("what_is_happening", {"time": 9999}),
            ("what_is_happening", {"time": 10, "window": 0}),
            ("what_is_happening", {"time": 10, "window": 601}),
            ("timeline_index", {"step": 0.5}),
            ("timeline_index", {"start": 50, "end": 10}),
        ):
            assert (await mcp.call_tool(tool, {"asset": asset.id, **args})).is_error, (tool, args)
        assert (await mcp.call_tool("what_is_happening", {"asset": "a_0000000000000000", "time": 1})).is_error


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_a_real_stdio_subprocess(planted, anyio_backend):
    root, asset = planted
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(root)],
    )
    async with Client(params) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert {"what_is_happening", "timeline_index"} <= names
        found = await _call(mcp, "what_is_happening", asset=asset.id, time=100)
        line = await _call(mcp, "timeline_index", asset=asset.id, step=100)
        assert found["speech"]["word_count"] == 10 and found["audio"]["class"] == "speech"
    client = TestClient(create_app(root, DEFAULTS))
    assert client.get(f"/api/v1/assets/{asset.id}/at", params={"time": 100}).json() == found
    assert client.get(f"/api/v1/assets/{asset.id}/timeline", params={"step": 100}).json() == line
    assert _cli(root, "at", asset.id, "100") == found
