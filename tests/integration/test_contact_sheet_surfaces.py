"""Contact sheets through the HTTP API, the CLI and MCP (in memory and over a real stdio subprocess), with real
ffmpeg on a clip whose colour changes every second (SPL-46)."""

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from test_contact_sheet import decode, sheet_clip, tile_colour

from split_video import assets, contact_sheet
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
    sheet_clip(path)
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


def _check_tiles(data, tiles, columns, tile_w, tile_h, rows):
    image = decode(data)
    assert image.shape == (rows * tile_h, columns * tile_w, 3)

    class L:
        tile_width, tile_height = tile_w, tile_h

    for t in tiles:
        assert tile_colour(image, type("T", (), t)(), L) == int(t["time"]), t


# --- HTTP -----------------------------------------------------------------------------------------


def test_api_returns_one_image_with_the_tile_map_in_headers(workspace):
    _, asset = _asset(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/contact-sheet"
    r = client.get(base)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert decode(r.content).shape == (540, 1280, 3)
    h = r.headers
    assert (h["x-sheet-columns"], h["x-sheet-rows"], h["x-sheet-tile-width"], h["x-sheet-tile-height"]) == (
        "4",
        "3",
        "320",
        "180",
    )
    assert (h["x-sheet-width"], h["x-sheet-height"], h["x-sheet-source"]) == ("1280", "540", "original")
    assert h["x-sheet-duration"] == "12" and h["x-sheet-start"] == "0" and h["x-sheet-end"] == "12"
    assert h["x-sheet-times"] == ",".join(f"{i + 0.5:g}" for i in range(12))
    assert h["cache-control"] == "private, max-age=300" and h["etag"]
    assert client.get(base).headers["etag"] == h["etag"]
    assert client.get(base, params={"count": 6}).headers["etag"] != h["etag"]

    png = client.get(base, params={"start": 3, "end": 9, "count": 3, "columns": 3, "format": "png", "tile_width": 200})
    assert png.headers["content-type"] == "image/png" and png.headers["x-sheet-times"] == "4,6,8"
    assert decode(png.content).shape == (112, 600, 3)


def test_api_info_is_the_tile_map_without_an_image(workspace):
    _, asset = _asset(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    r = client.get(f"/api/v1/assets/{asset.id}/contact-sheet/info", params={"start": 2, "end": 8, "count": 6})
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    info = r.json()
    assert (info["columns"], info["rows"], info["width"], info["height"]) == (3, 2, 960, 360)
    assert info["tiles"][4] == {"index": 4, "time": 6.5, "x": 320, "y": 180}
    full = client.get(f"/api/v1/assets/{asset.id}/contact-sheet", params={"start": 2, "end": 8, "count": 6})
    assert full.headers["x-sheet-times"] == ",".join(f"{t['time']:g}" for t in info["tiles"])


def test_api_errors(workspace):
    media, asset = _asset(workspace)
    song = assets.register_path(media, "song.mp3")
    client = TestClient(create_app(workspace, DEFAULTS))
    for suffix in ("contact-sheet", "contact-sheet/info"):
        base = f"/api/v1/assets/{asset.id}/{suffix}"
        assert client.get(f"/api/v1/assets/a_0000000000000000/{suffix}").status_code == 404
        late = client.get(base, params={"end": 99})
        assert late.status_code == 422 and "12 s long" in late.json()["detail"]
        assert client.get(base, params={"start": 5, "end": 5}).status_code == 422
        for bad in ({"count": 0}, {"count": 65}, {"columns": 99}, {"tile_width": 10}, {"source": "proxy"}):
            r = client.get(base, params=bad)
            assert r.status_code == 400 and "detail" in r.json(), bad
        wide = client.get(base, params={"count": 9, "columns": 9, "tile_width": 400})
        assert wide.status_code == 400 and "2560" in wide.json()["detail"]
        audio = client.get(f"/api/v1/assets/{song.id}/{suffix}")
        assert audio.status_code == 422 and "audio-only" in audio.json()["detail"]
    assert client.get(f"/api/v1/assets/{asset.id}/contact-sheet", params={"format": "gif"}).status_code == 400


def test_openapi_describes_the_routes(workspace):
    schema = create_app(workspace, DEFAULTS).openapi()
    ok = schema["paths"]["/api/v1/assets/{asset_id}/contact-sheet"]["get"]["responses"]["200"]["content"]
    assert set(ok) == {"image/jpeg", "image/png"} and ok["image/png"]["schema"]["format"] == "binary"
    assert "/api/v1/assets/{asset_id}/contact-sheet/info" in schema["paths"]


# --- CLI ------------------------------------------------------------------------------------------


def test_cli_writes_one_image_by_path_and_by_asset_id(workspace, tmp_path):
    out = tmp_path / "sheet.jpg"
    r = runner.invoke(app, ["contact-sheet", "shows/colours.mp4", "--root", str(workspace), "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert decode(out.read_bytes()).shape == (540, 1280, 3) and "1280x540" in r.output and "12 frames" in r.output
    _, asset = _asset(workspace)
    png = tmp_path / "s.png"
    r = runner.invoke(
        app,
        ["contact-sheet", asset.id, "--root", str(workspace), "--out", str(png), "--start", "0:03", "--end", "0:09"]
        + ["--count", "3", "--columns", "3", "--tile-width", "200", "--json"],
    )
    assert r.exit_code == 0, r.output
    info = json.loads(r.stdout)
    assert info["format"] == "png" and info["out"] == str(png) and [t["time"] for t in info["tiles"]] == [4, 6, 8]
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    _check_tiles(png.read_bytes(), info["tiles"], 3, 200, 112, 1)


def test_cli_default_name_is_in_the_cwd_not_the_workspace(workspace, tmp_path, monkeypatch):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    r = runner.invoke(app, ["contact-sheet", "shows/colours.mp4", "--root", str(workspace), "--start", "2"])
    assert r.exit_code == 0, r.output
    assert [p.name for p in cwd.iterdir()] == ["colours-sheet-2s.jpg"]
    assert not list(workspace.rglob("*.jpg"))


def test_cli_errors(workspace, tmp_path):
    out = str(tmp_path / "e.jpg")
    base = ["--root", str(workspace), "--out", out]
    late = runner.invoke(app, ["contact-sheet", "shows/colours.mp4", "--end", "99", *base])
    assert late.exit_code == 1 and "12 s long" in late.output
    assert runner.invoke(app, ["contact-sheet", "shows/colours.mp4", "--start", "soon", *base]).exit_code == 2
    assert runner.invoke(app, ["contact-sheet", "shows/colours.mp4", "--count", "100", *base]).exit_code == 2
    assert runner.invoke(app, ["contact-sheet", "song.mp3", *base]).exit_code == 1
    assert runner.invoke(app, ["contact-sheet", "nope.mp4", *base]).exit_code == 2
    wide = runner.invoke(
        app, ["contact-sheet", "shows/colours.mp4", "--count", "9", "--columns", "9", "--tile-width", "400", *base]
    )
    assert wide.exit_code == 1 and "2560" in wide.output
    assert not Path(out).exists()


# --- MCP ------------------------------------------------------------------------------------------


def _check_result(result, times, columns, rows, tile_w, tile_h):
    assert not result.is_error, result.content
    images = [c for c in result.content if c.type == "image"]
    texts = [c for c in result.content if c.type == "text"]
    assert len(images) == 1 and len(texts) == 1 and images[0].mime_type == "image/jpeg"
    data = base64.b64decode(images[0].data)
    info = json.loads(texts[0].text)
    assert info["times"] == times and (info["columns"], info["rows"]) == (columns, rows)
    assert (info["tile_width"], info["tile_height"]) == (tile_w, tile_h) and info["duration"] == 12.0
    assert "tiles" not in info and "bytes" not in info
    tiles = [{"time": t, "x": (i % columns) * tile_w, "y": (i // columns) * tile_h} for i, t in enumerate(times)]
    _check_tiles(data, tiles, columns, tile_w, tile_h, rows)
    return info


@pytest.mark.anyio
async def test_mcp_in_memory_one_image_and_a_tile_map_and_errors(workspace):
    _, asset = _asset(workspace)
    song = assets.register_path(LocalMediaStore(workspace.resolve()), "song.mp3")
    async with Client(create_server(workspace)) as client:
        assert "get_contact_sheet" in {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("get_contact_sheet", {"asset": asset.id})
        _check_result(result, [i + 0.5 for i in range(12)], 4, 3, 320, 180)
        result = await client.call_tool(
            "get_contact_sheet", {"asset": asset.id, "start": 4, "end": 10, "count": 6, "tile_width": 160}
        )
        _check_result(result, [4.5, 5.5, 6.5, 7.5, 8.5, 9.5], 3, 2, 160, 90)
        late = await client.call_tool("get_contact_sheet", {"asset": asset.id, "end": 60})
        assert late.is_error and "12 s long" in late.content[0].text
        assert (await client.call_tool("get_contact_sheet", {"asset": song.id})).is_error
        assert (await client.call_tool("get_contact_sheet", {"asset": asset.id, "count": 500})).is_error
        assert (await client.call_tool("get_contact_sheet", {"asset": "a_0000000000000000"})).is_error


@pytest.mark.anyio
async def test_mcp_over_a_real_stdio_subprocess(workspace):
    _, asset = _asset(workspace)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(workspace)],
    )
    async with Client(params) as client:
        result = await client.call_tool("get_contact_sheet", {"asset": asset.id, "count": 4, "tile_width": 240})
        _check_result(result, [1.5, 4.5, 7.5, 10.5], 2, 2, 240, 136)
        assert (await client.call_tool("get_contact_sheet", {"asset": asset.id, "start": 20})).is_error


def test_engine_error_classes_are_the_frames_ones():
    from split_video import frames

    assert issubclass(contact_sheet.SheetFontError, frames.FrameError)
