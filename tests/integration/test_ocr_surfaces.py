"""On-screen text through the HTTP API (as a job), the CLI and MCP (in memory and over a real stdio subprocess), on a
real rendered clip (tests/screen_clip.py). The engine is faked in process (`ClipEngine` returns each scene's known text
for the real decoded frames) and, for the stdio server, by a stand-in `tesseract` program on PATH; the real tesseract
is in tests/unit/test_ocr_real.py (SPL-98)."""

import json
import os
import shutil
import stat
import subprocess
import sys
import time

import pytest
from fake_ocr import ClipEngine
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
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=duration=2", str(root / "song.mp3")],
        capture_output=True,
        check=True,
    )
    return root


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch):
    monkeypatch.setattr(ocr, "default_engine", lambda: ClipEngine())


@pytest.fixture
def no_tesseract(monkeypatch, fake_engine):
    monkeypatch.undo()  # the fake out, the real availability check back in...
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "tesseract" else real(name, *a, **k))


def _wait(client, job_id, timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _coding(root):
    media = LocalMediaStore(root.resolve())
    return media, assets.register_path(media, "streams/coding.mp4")


def test_api_ocr_job_then_read_and_search(workspace):
    _, asset = _coding(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    assert (
        client.get(f"{base}/ocr").status_code == 404
        and client.get(f"{base}/ocr/search", params={"q": "x"}).status_code == 404
    )
    assert client.get(base).json()["ocr"] is False

    job = _wait(client, client.post(f"{base}/ocr", json={"interval": 6}).json()["job_id"])
    assert job["kind"] == "ocr" and job["state"] == "done"
    assert job["completed"] == job["total"] == job["result"]["frames"] == 6
    result = job["result"]
    assert result["made"] is True and result["ocr_frames"] == 3 and result["skipped_unchanged"] == 3
    assert result["engine"] == "fake-tesseract 5.0" and result["interval"] == 6 and result["scenes_used"] is False

    page = client.get(f"{base}/ocr").json()
    assert page["asset"] == asset.id and page["total_in_range"] == 3 and page["next_cursor"] is None
    assert [i["time"] for i in page["items"]] == [0.0, 12.0, 24.0] and page["items"][1]["until"] == 24.0
    assert "FAILED" in page["items"][1]["text"] and page["items"][0]["lines"][0].startswith("# split_video")
    first = client.get(f"{base}/ocr", params={"limit": 1}).json()
    second = client.get(f"{base}/ocr", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [first["items"][0]["time"], second["items"][0]["time"]] == [0.0, 12.0]
    cut = client.get(f"{base}/ocr", params={"start": 30, "end": 31}).json()
    assert [i["time"] for i in cut["items"]] == [24.0]

    found = client.get(f"{base}/ocr/search", params={"q": PLANTED}).json()
    assert found["total_matches"] == 1 and found["hits"][0]["time"] == 12.0 and found["hits"][0]["source"] == "ocr"
    assert client.get(f"{base}/ocr/search", params={"q": "silence", "start": 0, "end": 20}).json()["total_matches"] == 0
    assert client.get(base).json()["ocr"] is True
    assert [a["ocr"] for a in client.get("/api/v1/assets").json()["assets"] if a["has_video"]] == [True]

    assert _wait(client, client.post(f"{base}/ocr", json={"interval": 6}).json()["job_id"])["result"]["made"] is False
    assert _wait(client, client.post(f"{base}/ocr", json={"interval": 12}).json()["job_id"])["result"]["made"] is True


def test_api_errors(workspace):
    media, asset = _coding(workspace)
    song = assets.register_path(media, "song.mp3")
    client = TestClient(create_app(workspace, DEFAULTS))
    assert client.post(f"/api/v1/assets/{song.id}/ocr").status_code == 422
    assert client.post("/api/v1/assets/a_0000000000000000/ocr").status_code == 404
    assert client.get("/api/v1/assets/a_0000000000000000/ocr").status_code == 404
    assert client.get(f"/api/v1/assets/{song.id}/ocr").status_code == 404
    for body in ({"interval": 0}, {"max_frames": 0}, {"max_frames": 10**6}):
        assert client.post(f"/api/v1/assets/{asset.id}/ocr", json=body).status_code == 422
    assert client.get("/api/v1/jobs").json()["jobs"] == []
    base = f"/api/v1/assets/{asset.id}"
    _wait(client, client.post(f"{base}/ocr", json={"interval": 12}).json()["job_id"])
    for url, params in (
        (f"{base}/ocr", {"limit": 0}),
        (f"{base}/ocr", {"cursor": "bogus"}),
        (f"{base}/ocr", {"start": 5, "end": 5}),
        (f"{base}/ocr/search", {"q": ""}),
        (f"{base}/ocr/search", {"q": "x" * 501}),
        (f"{base}/ocr/search", {"q": "x", "limit": 999}),
    ):
        assert client.get(url, params=params).status_code == 422, (url, params)
    assert client.get(f"{base}/ocr/search").status_code == 422  # q is required


def test_api_without_tesseract_is_501_with_the_hint_and_queues_nothing(workspace, no_tesseract):
    _, asset = _coding(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    got = client.post(f"/api/v1/assets/{asset.id}/ocr")
    assert got.status_code == 501 and got.json()["detail"].startswith(ocr.UNAVAILABLE)
    assert "WITH_OCR=1" in got.json()["detail"] and client.get("/api/v1/jobs").json()["jobs"] == []


def test_api_cancel_stops_a_running_job_and_stores_nothing(workspace, monkeypatch):
    media, asset = _coding(workspace)
    started = []
    monkeypatch.setattr(ocr, "default_engine", lambda: ClipEngine(delay=0.5, on_read=started.append))
    client = TestClient(create_app(workspace, DEFAULTS))
    job_id = client.post(f"/api/v1/assets/{asset.id}/ocr", json={"interval": 1}).json()["job_id"]
    deadline = time.time() + 30
    while not started and time.time() < deadline:
        time.sleep(0.02)
    asked = time.time()
    client.post(f"/api/v1/jobs/{job_id}/cancel")
    job = _wait(client, job_id)
    assert job["state"] == "cancelled" and time.time() - asked < 10
    assert client.get(f"/api/v1/assets/{asset.id}/ocr").status_code == 404 and ocr.load(media, asset.id) is None


def test_cli_ocr_by_path_and_by_asset_id_then_text_and_search(workspace):
    out = runner.invoke(app, ["ocr", "streams/coding.mp4", "--root", str(workspace), "--interval", "6", "--json"])
    assert out.exit_code == 0, out.output
    result = json.loads(out.stdout)
    assert result["made"] is True and result["ocr_frames"] == 3 and result["asset"].startswith("a_")
    asset = result["asset"]
    again = json.loads(runner.invoke(app, ["ocr", asset, "--root", str(workspace), "--interval", "6", "--json"]).stdout)
    assert again["made"] is False and {**again, "made": True} == result
    forced = json.loads(
        runner.invoke(app, ["ocr", asset, "--root", str(workspace), "--interval", "6", "--force", "--json"]).stdout
    )
    assert forced["made"] is True
    plain = runner.invoke(app, ["ocr", asset, "--root", str(workspace), "--interval", "6"])
    assert plain.exit_code == 0 and "Already read" in plain.output and "3 frame(s) read" in plain.output
    assert "no scene data" in plain.output and "ocr-search" in plain.output

    text = json.loads(
        runner.invoke(app, ["ocr-text", asset, "--root", str(workspace), "--limit", "2", "--json"]).stdout
    )
    assert [i["time"] for i in text["items"]] == [0.0, 12.0] and text["next_cursor"]
    more = json.loads(
        runner.invoke(
            app,
            ["ocr-text", asset, "--root", str(workspace), "--limit", "2", "--cursor", text["next_cursor"], "--json"],
        ).stdout
    )
    assert [i["time"] for i in more["items"]] == [24.0] and more["next_cursor"] is None
    printed = runner.invoke(app, ["ocr-text", asset, "--root", str(workspace), "--start", "13", "--end", "14"])
    assert printed.exit_code == 0 and "FAILED" in printed.output and "1 of 1 in range" in printed.output

    hits = json.loads(runner.invoke(app, ["ocr-search", asset, PLANTED, "--root", str(workspace), "--json"]).stdout)
    assert hits["total_matches"] == 1 and hits["hits"][0]["time"] == 12.0
    shown = runner.invoke(app, ["ocr-search", "streams/coding.mp4", "silence detection", "--root", str(workspace)])
    assert shown.exit_code == 0 and "silence detection" in shown.output and "1 of 1 matches" in shown.output


def test_cli_errors_and_audio_only(workspace):
    assert runner.invoke(app, ["ocr", "missing.mp4", "--root", str(workspace)]).exit_code != 0
    unknown = runner.invoke(app, ["ocr", "a_0000000000000000", "--root", str(workspace)])
    assert unknown.exit_code == 1 and "unknown asset" in unknown.output
    assert runner.invoke(app, ["ocr", "streams/coding.mp4", "--root", str(workspace), "--interval", "0"]).exit_code == 2
    assert (
        runner.invoke(app, ["ocr", "streams/coding.mp4", "--root", str(workspace), "--max-frames", "0"]).exit_code == 2
    )
    song = runner.invoke(app, ["ocr", "song.mp3", "--root", str(workspace), "--json"])
    assert song.exit_code == 0 and json.loads(song.stdout)["skipped"] == "audio-only asset"
    assert "Skipped" in runner.invoke(app, ["ocr", "song.mp3", "--root", str(workspace)]).output
    empty = runner.invoke(app, ["ocr-search", "streams/coding.mp4", "x", "--root", str(workspace)])
    assert empty.exit_code == 1 and "split-video ocr" in empty.output and "ocr_asset" in empty.output
    assert runner.invoke(app, ["ocr-text", "streams/coding.mp4", "--root", str(workspace)]).exit_code == 1
    runner.invoke(app, ["ocr", "streams/coding.mp4", "--root", str(workspace), "--interval", "12"])
    assert runner.invoke(app, ["ocr-search", "streams/coding.mp4", "   ", "--root", str(workspace)]).exit_code == 1


def test_cli_without_tesseract_exits_4_with_the_hint(workspace, no_tesseract):
    out = runner.invoke(app, ["ocr", "streams/coding.mp4", "--root", str(workspace)])
    assert out.exit_code == 4 and "apt install tesseract-ocr" in out.output and "WITH_OCR=1" in out.output
    assert ocr.load(LocalMediaStore(workspace.resolve()), _coding(workspace)[1].id) is None


async def _call(client, tool, /, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_tools_and_the_same_json_on_every_surface(workspace, anyio_backend):
    media, asset = _coding(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    async with Client(create_server(workspace)) as mcp:
        await mcp.call_tool("scan_library", {})
        listed = (await mcp.call_tool("list_assets", {})).structured_content["assets"]
        video = next(a for a in listed if a["has_video"])
        song = next(a for a in listed if not a["has_video"])
        assert video["ocr"] is False and video["id"] == asset.id
        for tool, args in (("get_ocr", {"asset": asset.id}), ("ocr_search", {"asset": asset.id, "query": "x"})):
            result = await mcp.call_tool(tool, args)
            assert result.is_error and "ocr_asset" in result.content[0].text

        done = await _call(mcp, "ocr_asset", asset=asset.id, interval=6)
        assert done["made"] is True and done["ocr_frames"] == 3 and done["engine"] == "fake-tesseract 5.0"
        assert (await _call(mcp, "ocr_asset", asset=asset.id, interval=6))["made"] is False
        assert (await _call(mcp, "ocr_asset", asset=song["id"]))["skipped"] == "audio-only asset"
        assert next(a for a in (await _call(mcp, "list_assets"))["assets"] if a["id"] == asset.id)["ocr"] is True
        for args in ({"asset": asset.id, "interval": 0.1}, {"asset": asset.id, "max_frames": 0}):
            assert (await mcp.call_tool("ocr_asset", args)).is_error

        # the same JSON from HTTP, the CLI and MCP
        http_page = client.get(f"{base}/ocr", params={"limit": 2}).json()
        assert await _call(mcp, "get_ocr", asset=asset.id, limit=2) == http_page
        cli_page = json.loads(
            runner.invoke(app, ["ocr-text", asset.id, "--root", str(workspace), "--limit", "2", "--json"]).stdout
        )
        assert cli_page == http_page
        window = {"start": 13.0, "end": 30.0}
        assert await _call(mcp, "get_ocr", asset=asset.id, **window) == client.get(f"{base}/ocr", params=window).json()

        http_hits = client.get(f"{base}/ocr/search", params={"q": "assertion error", "context": 2}).json()
        assert http_hits["total_matches"] >= 1
        assert await _call(mcp, "ocr_search", asset=asset.id, query="assertion error", context=2) == http_hits
        cli_hits = json.loads(
            runner.invoke(
                app, ["ocr-search", asset.id, "assertion error", "--root", str(workspace), "--context", "2", "--json"]
            ).stdout
        )
        assert cli_hits == http_hits
        assert {
            "source",
            "time",
            "end",
            "score",
            "text",
            "context_before",
            "context_after",
            "matched_tokens",
            "frame",
        } == set(http_hits["hits"][0])

        for tool, args in (
            ("get_ocr", {"asset": asset.id, "limit": 0}),
            ("get_ocr", {"asset": "a_0000000000000000"}),
            ("ocr_search", {"asset": asset.id, "query": ""}),
            ("ocr_search", {"asset": asset.id, "query": "x", "cursor": "nope"}),
        ):
            assert (await mcp.call_tool(tool, args)).is_error, (tool, args)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_without_tesseract_is_a_tool_error_with_the_hint(workspace, no_tesseract, anyio_backend):
    _, asset = _coding(workspace)
    async with Client(create_server(workspace)) as mcp:
        result = await mcp.call_tool("ocr_asset", {"asset": asset.id})
        assert result.is_error and "apt install tesseract-ocr" in result.content[0].text


FAKE_TESSERACT = """#!{python}
import sys
if "--version" in sys.argv:
    sys.stderr.write("tesseract 5.5.0\\n")
else:
    sys.stdout.write("stand-in program: {planted} line two\\n")
"""


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_a_real_stdio_subprocess_with_a_stand_in_tesseract_program(workspace, tmp_path, anyio_backend):
    """The real TesseractEngine (ffmpeg preparing the frames, a subprocess per frame) with only the OCR program
    replaced, in a server started like an agent would start it."""
    _, asset = _coding(workspace)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    program = bin_dir / "tesseract"
    program.write_text(FAKE_TESSERACT.format(python=sys.executable, planted=PLANTED))
    program.chmod(program.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(workspace)],
        env=env,
    )
    async with Client(params) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert {"ocr_asset", "get_ocr", "ocr_search"} <= names
        done = await _call(mcp, "ocr_asset", asset=asset.id, interval=12)
        assert done["engine"] == "tesseract 5.5.0" and done["made"] is True and done["frames"] == 3
        hit = await _call(mcp, "ocr_search", asset=asset.id, query="test cut burst keeps strongest")
        assert hit["total_matches"] == 3 and hit["hits"][0]["source"] == "ocr"  # one per changed screen, same text
        assert sorted(h["time"] for h in hit["hits"]) == [0.0, 12.0, 24.0]
        page = await _call(mcp, "get_ocr", asset=asset.id)
        assert page["total_in_range"] == 3 and page["items"][0]["until"] == 12.0
        assert (await mcp.call_tool("ocr_search", {"asset": asset.id, "query": ""})).is_error
    # the same stored text reads identically over HTTP
    client = TestClient(create_app(workspace, DEFAULTS))
    assert client.get(f"/api/v1/assets/{asset.id}/ocr").json() == page
