"""Frame extraction through the HTTP API, the CLI and MCP (in memory and over a real stdio subprocess), with real
ffmpeg on a clip whose colour changes every second (SPL-45)."""

import base64
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
import subprocess

from test_frames import colour_clip, decode, dominant

from split_video import assets, proxy
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()
pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("clip") / "colours.mp4"
    colour_clip(path)
    return path


@pytest.fixture
def workspace(tmp_path, clip):
    root = tmp_path / "ws"
    (root / "shows").mkdir(parents=True)
    (root / "shows" / "colours.mp4").write_bytes(clip.read_bytes())
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", str(root / "song.mp3")], check=True
    )
    return root


def _asset(root, path="shows/colours.mp4"):
    media = LocalMediaStore(root.resolve())
    return media, assets.register_path(media, path)


# --- HTTP -----------------------------------------------------------------------------------------


def test_api_returns_the_image_with_headers(workspace):
    _, asset = _asset(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/frame"
    r = client.get(base, params={"time": 2.5})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert dominant(r.content) == "blue" and decode(r.content)[:2] == (1280, 720)
    assert r.headers["cache-control"] == "private, max-age=300" and r.headers["etag"]
    assert r.headers["x-frame-source"] == "original" and r.headers["x-frame-duration"] == "6"
    assert (r.headers["x-frame-width"], r.headers["x-frame-height"]) == ("1280", "720")
    assert 2.5 <= float(r.headers["x-frame-time"]) < 2.61
    assert client.get(base, params={"time": 2.5}).headers["etag"] == r.headers["etag"]

    png = client.get(base, params={"time": 4.5, "format": "png", "width": 320})
    assert png.headers["content-type"] == "image/png" and dominant(png.content) == "magenta"
    assert decode(png.content)[:2] == (320, 180) and png.headers["x-frame-width"] == "320"
    assert dominant(client.get(base, params={"time": 6}).content) == "cyan"  # exactly the end


def test_api_proxy_and_original_sources(workspace):
    media, asset = _asset(workspace)
    proxy.prepare(media, asset.id)
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/frame"
    auto = client.get(base, params={"time": 3.5})
    assert auto.headers["x-frame-source"] == "proxy" and decode(auto.content)[0] == 640
    orig = client.get(base, params={"time": 3.5, "source": "original"})
    assert orig.headers["x-frame-source"] == "original" and decode(orig.content)[0] == 1280
    assert auto.headers["etag"] != orig.headers["etag"]


def test_api_errors(workspace):
    media, asset = _asset(workspace)
    song = assets.register_path(media, "song.mp3")
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/frame"
    assert client.get("/api/v1/assets/a_0000000000000000/frame", params={"time": 1}).status_code == 404
    late = client.get(base, params={"time": 99})
    assert late.status_code == 422 and "6 s long" in late.json()["detail"]
    assert client.get(base, params={"time": -1}).status_code == 422
    assert client.get(base).status_code == 422  # time is required
    for bad in ({"format": "gif"}, {"source": "proxy"}, {"width": 5000}, {"height": 0}, {"quality": 0}):
        r = client.get(base, params={"time": 1, **bad})
        assert r.status_code == 400 and "detail" in r.json(), bad
    audio = client.get(f"/api/v1/assets/{song.id}/frame", params={"time": 1})
    assert audio.status_code == 422 and "audio-only" in audio.json()["detail"]


def test_openapi_describes_a_binary_image(workspace):
    schema = create_app(workspace, DEFAULTS).openapi()
    ok = schema["paths"]["/api/v1/assets/{asset_id}/frame"]["get"]["responses"]["200"]["content"]
    assert set(ok) == {"image/jpeg", "image/png"} and ok["image/png"]["schema"]["format"] == "binary"


# --- CLI ------------------------------------------------------------------------------------------


def test_cli_writes_the_file_by_path_and_by_asset_id(workspace, tmp_path):
    out = tmp_path / "shot.jpg"
    r = runner.invoke(app, ["frame", "shows/colours.mp4", "2.5", "--root", str(workspace), "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert dominant(out.read_bytes()) == "blue" and "1280x720" in r.output
    _, asset = _asset(workspace)
    png = tmp_path / "shot.png"  # the suffix picks the format
    r = runner.invoke(
        app,
        ["frame", asset.id, "0:04", "--root", str(workspace), "--out", str(png), "--width", "320", "--json"],
    )
    info = json.loads(r.stdout)
    assert info["format"] == "png" and info["width"] == 320 and info["out"] == str(png)
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and dominant(png.read_bytes()) == "magenta"
    # --source and --format
    r = runner.invoke(
        app,
        ["frame", asset.id, "1", "--root", str(workspace), "--out", str(tmp_path / "x"), "--format", "png", "--json"],
    )
    assert json.loads(r.stdout)["format"] == "png"


def test_cli_default_name_is_in_the_cwd_not_the_workspace(workspace, tmp_path, monkeypatch):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    r = runner.invoke(app, ["frame", "shows/colours.mp4", "1.5", "--root", str(workspace)])
    assert r.exit_code == 0, r.output
    assert [p.name for p in cwd.iterdir()] == ["colours-1.5s.jpg"]
    assert dominant((cwd / "colours-1.5s.jpg").read_bytes()) == "green"
    assert not list(workspace.rglob("*.jpg"))


def test_cli_errors(workspace, tmp_path):
    out = str(tmp_path / "e.jpg")
    base = ["--root", str(workspace), "--out", out]
    late = runner.invoke(app, ["frame", "shows/colours.mp4", "10", *base])
    assert late.exit_code == 1 and "6 s long" in late.output
    assert runner.invoke(app, ["frame", "shows/colours.mp4", "soon", *base]).exit_code == 2
    assert runner.invoke(app, ["frame", "song.mp3", "1", *base]).exit_code == 1
    assert runner.invoke(app, ["frame", "nope.mp4", "1", *base]).exit_code == 2
    assert runner.invoke(app, ["frame", "shows/colours.mp4", "1", *base, "--source", "proxy"]).exit_code == 1
    assert not Path(out).exists()


# --- MCP ------------------------------------------------------------------------------------------


def _check_image_result(result, expected_colour, size):
    assert not result.is_error, result.content
    images = [c for c in result.content if c.type == "image"]
    texts = [c for c in result.content if c.type == "text"]
    assert len(images) == 1 and len(texts) == 1
    assert images[0].mime_type == "image/jpeg"
    data = base64.b64decode(images[0].data)
    assert data[:3] == b"\xff\xd8\xff" and decode(data)[:2] == size and dominant(data) == expected_colour
    info = json.loads(texts[0].text)
    assert info["width"] == size[0] and info["height"] == size[1] and info["source"] == "original"
    assert "bytes" not in info and info["duration"] == 6.0
    return info


@pytest.mark.anyio
async def test_mcp_in_memory_image_block_and_errors(workspace):
    _, asset = _asset(workspace)
    song = assets.register_path(LocalMediaStore(workspace.resolve()), "song.mp3")
    async with Client(create_server(workspace)) as client:
        assert "get_frame" in {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("get_frame", {"asset": asset.id, "time": 3.5, "width": 320})
        _check_image_result(result, "yellow", (320, 180))
        late = await client.call_tool("get_frame", {"asset": asset.id, "time": 60})
        assert late.is_error and "6 s long" in late.content[0].text
        audio = await client.call_tool("get_frame", {"asset": song.id, "time": 1})
        assert audio.is_error and "audio-only" in audio.content[0].text
        bad = await client.call_tool("get_frame", {"asset": asset.id, "time": 1, "format": "bmp"})
        assert bad.is_error
        missing = await client.call_tool("get_frame", {"asset": "a_0000000000000000", "time": 1})
        assert missing.is_error


@pytest.mark.anyio
async def test_mcp_over_a_real_stdio_subprocess(workspace):
    _, asset = _asset(workspace)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(workspace)],
    )
    async with Client(params) as client:
        result = await client.call_tool("get_frame", {"asset": asset.id, "time": 1.2, "width": 640, "format": "jpeg"})
        info = _check_image_result(result, "green", (640, 360))
        assert 1.2 <= info["time"] < 1.31
        late = await client.call_tool("get_frame", {"asset": asset.id, "time": 7})
        assert late.is_error
