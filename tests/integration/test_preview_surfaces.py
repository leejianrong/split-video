"""Preview frames through the HTTP API, the CLI and MCP, with real ffmpeg (SPL-69)."""

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_frames import decode, dominant

from split_video import timeline_service
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()
pytestmark = pytest.mark.filterwarnings("ignore")
INFO_KEYS = {
    "project", "revision", "time", "requested_time", "width", "height", "mime", "format", "source", "duration"
}  # fmt: skip


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def workspace(tmp_path):
    for name, colour in (("red", "red"), ("blue", "blue")):
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={colour}:s=640x360:r=25:d=3"]
        assert subprocess.run([*cmd, "-pix_fmt", "yuv420p", str(tmp_path / f"{name}.mp4")], check=False).returncode == 0
    store, media = LocalProjectStore(tmp_path.resolve()), LocalMediaStore(tmp_path.resolve())
    timeline_service.create_project(store, "show")
    timeline_service.add_clip(store, media, "show", "red.mp4")
    timeline_service.add_clip(store, media, "show", "blue.mp4")
    return tmp_path


URL = "/api/v1/compose/projects/show/preview-frame"


def test_api_returns_the_image_with_headers_and_a_revision_etag(workspace):
    client = TestClient(create_app(workspace, DEFAULTS))
    r = client.get(URL, params={"time": 1.0, "width": 320})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert dominant(r.content) == "red" and decode(r.content)[:2] == (320, 180)
    assert r.headers["cache-control"] == "private, no-cache"
    h = r.headers
    assert (h["x-preview-width"], h["x-preview-height"], h["x-preview-duration"]) == ("320", "180", "6")
    assert h["x-preview-revision"] == "2" and float(h["x-preview-time"]) == pytest.approx(1.0, abs=0.04)
    assert dominant(client.get(URL, params={"time": 4.5}).content) == "blue"

    again = client.get(URL, params={"time": 1.0, "width": 320})
    assert again.headers["etag"] == h["etag"]
    assert client.get(URL, params={"time": 1.0, "width": 321}).headers["etag"] != h["etag"]
    cached = client.get(URL, params={"time": 1.0, "width": 320}, headers={"If-None-Match": h["etag"]})
    assert cached.status_code == 304 and not cached.content

    client.post("/api/v1/compose/projects/show/clips", json={"path": "red.mp4"})  # a new revision, a new key
    moved = client.get(URL, params={"time": 1.0, "width": 320}, headers={"If-None-Match": h["etag"]})
    assert moved.status_code == 200 and moved.headers["etag"] != h["etag"]
    assert moved.headers["x-preview-revision"] == "3"


def test_api_png_and_errors(workspace):
    client = TestClient(create_app(workspace, DEFAULTS))
    png = client.get(URL, params={"time": 1, "format": "png", "source": "original"})
    assert png.headers["content-type"] == "image/png" and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/api/v1/compose/projects/nope/preview-frame", params={"time": 1}).status_code == 404
    late = client.get(URL, params={"time": 99})
    assert late.status_code == 422 and "6 s long" in late.json()["detail"]
    assert late.json()["code"] == "time.out_of_range"
    assert client.get(URL, params={"time": -1}).status_code == 422
    assert client.get(URL).status_code == 422  # time is required
    assert client.get(URL, params={"time": 1, "preset": "nope"}).status_code == 422
    for bad in ({"format": "gif"}, {"source": "proxy"}, {"width": 5000}, {"quality": 0}):
        r = client.get(URL, params={"time": 1, **bad})
        assert r.status_code == 400 and "detail" in r.json(), bad


def test_openapi_describes_a_binary_image(workspace):
    schema = create_app(workspace, DEFAULTS).openapi()
    ok = schema["paths"]["/api/v1/compose/projects/{name}/preview-frame"]["get"]["responses"]["200"]["content"]
    assert set(ok) == {"image/jpeg", "image/png"} and ok["image/png"]["schema"]["format"] == "binary"


def test_cli_writes_the_file_and_prints_json_with_path_and_revision(workspace, tmp_path):
    out = tmp_path / "shot.png"  # the suffix picks the format
    r = runner.invoke(
        app, ["compose", "preview-frame", "show", "0:04", "--root", str(workspace), "--out", str(out), "--width", "320"]
    )
    assert r.exit_code == 0, r.output
    info = json.loads(r.stdout)
    assert INFO_KEYS <= set(info) and info["out"] == str(out) and info["revision"] == 2
    assert (info["project"], info["format"], info["width"], info["height"]) == ("show", "png", 320, 180)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and dominant(out.read_bytes()) == "blue"
    assert info["bytes"] == out.stat().st_size


def test_cli_default_file_is_in_the_cwd_and_errors_exit_nonzero(workspace, tmp_path, monkeypatch):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    r = runner.invoke(app, ["compose", "preview-frame", "show", "1.5", "--root", str(workspace)])
    assert r.exit_code == 0, r.output
    assert [p.name for p in cwd.iterdir()] == ["show-1.5s.jpg"]
    late = runner.invoke(app, ["compose", "preview-frame", "show", "10", "--root", str(workspace)])
    assert late.exit_code == 1 and "6 s long" in late.output
    assert runner.invoke(app, ["compose", "preview-frame", "nope", "1", "--root", str(workspace)]).exit_code == 1
    assert runner.invoke(app, ["compose", "preview-frame", "show", "soon", "--root", str(workspace)]).exit_code == 2
    assert [p.name for p in cwd.iterdir()] == ["show-1.5s.jpg"]


@pytest.mark.anyio
async def test_mcp_returns_an_image_block_and_the_same_info(workspace):
    async with Client(create_server(workspace)) as client:
        assert "preview_frame" in {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("preview_frame", {"name": "show", "time": 4.5, "width": 320})
        assert not result.is_error, result.content
        images = [c for c in result.content if c.type == "image"]
        texts = [c for c in result.content if c.type == "text"]
        assert len(images) == 1 and len(texts) == 1 and images[0].mime_type == "image/jpeg"
        data = base64.b64decode(images[0].data)
        assert dominant(data) == "blue" and decode(data)[:2] == (320, 180)
        info = json.loads(texts[0].text)
        assert set(info) == INFO_KEYS and info["revision"] == 2 and info["project"] == "show"

        wide = await client.call_tool("preview_frame", {"name": "show", "time": 1, "width": 4000})
        assert json.loads(wide.content[-1].text)["width"] == 640  # the export is 640 wide: never above it
        late = await client.call_tool("preview_frame", {"name": "show", "time": 60})
        assert late.is_error and "6 s long" in late.content[0].text
        missing = await client.call_tool("preview_frame", {"name": "nope", "time": 1})
        assert missing.is_error


def test_api_cli_and_mcp_preview_the_videos_own_timeline(workspace, tmp_path):
    """The editor's project is a recording's own timeline, addressed by path (not a named project)."""
    client = TestClient(create_app(workspace, DEFAULTS))
    r = client.get("/api/v1/project/preview-frame", params={"path": "red.mp4", "time": 1, "width": 320})
    assert r.status_code == 200 and dominant(r.content) == "red" and decode(r.content)[:2] == (320, 180)
    assert r.headers["x-preview-revision"] == "0"
    cached = client.get(
        "/api/v1/project/preview-frame",
        params={"path": "red.mp4", "time": 1, "width": 320},
        headers={"If-None-Match": r.headers["etag"]},
    )
    assert cached.status_code == 304
    assert client.get("/api/v1/project/preview-frame", params={"path": "nope.mp4", "time": 1}).status_code == 404
    assert client.get("/api/v1/project/preview-frame", params={"path": "../x.mp4", "time": 1}).status_code == 400

    out = tmp_path / "video.jpg"
    cli = runner.invoke(
        app, ["compose", "preview-frame", "--video", "red.mp4", "1", "--root", str(workspace), "--out", str(out)]
    )
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.stdout)["revision"] == 0 and dominant(out.read_bytes()) == "red"
    assert runner.invoke(app, ["compose", "preview-frame", "1", "--root", str(workspace)]).exit_code == 2


@pytest.mark.anyio
async def test_mcp_previews_the_videos_own_timeline(workspace):
    async with Client(create_server(workspace)) as client:
        result = await client.call_tool("preview_frame", {"video": "red.mp4", "time": 1, "width": 320})
        assert not result.is_error, result.content
        image = next(c for c in result.content if c.type == "image")
        assert dominant(base64.b64decode(image.data)) == "red"
        both = await client.call_tool("preview_frame", {"name": "show", "video": "red.mp4", "time": 1})
        assert both.is_error
