"""Export modes over HTTP, the CLI and MCP with real ffmpeg on the local stores (SPL-117). Output is read back with
ffprobe: segment counts and durations, the timeline file named after the project, shorts at 1080x1920 with audio and
captions, and a short with a missing source failing alone."""

import json
import shutil
import subprocess
import time

import anyio
import pytest
from fake_transcript import document_from_words
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from split_video import assets, project, timeline_service, transcribe
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.ffmpeg import probe_media
from split_video.jobs import InProcessJobQueue
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.mcp_server import create_server
from split_video.store import SavedSegment

pytestmark = pytest.mark.anyio

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
RUNNER = CliRunner()
WORDS = [(0.5 + 0.4 * i, 0.5 + 0.4 * i + 0.3, w) for i, w in enumerate(["one", "two", "three", "four", "five.", "six"])]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ffmpeg(*args):
    result = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *args], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    path = tmp_path_factory.mktemp("export-source") / "gig.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    )  # fmt: skip
    return path


@pytest.fixture
def ws(source, tmp_path):
    """gig.mp4 split into three segments (the last excluded), its transcript, shorts `short1` and `short2`, and
    `orphan`, a short whose source file is then deleted."""
    shutil.copy(source, tmp_path / "gig.mp4")
    _ffmpeg(  # different content, so it is its own asset (ids come from the content)
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(tmp_path / "other.mp4"),
    )  # fmt: skip
    media, store = LocalMediaStore(tmp_path), LocalProjectStore(tmp_path)
    asset = assets.register_path(media, "gig.mp4")
    transcribe.save(media, asset, document_from_words(asset.id, WORDS))
    assets.register_path(media, "other.mp4")
    project.save_segments(
        store,
        "gig.mp4",
        [
            SavedSegment(0.0, 4.0, label="Opener"),
            SavedSegment(4.0, 8.0, label="Song", export_name="the-song"),
            SavedSegment(8.0, 12.0, included=False),
        ],
        media=media,
    )
    for name, (a, b) in {"short1": (1.0, 3.0), "short2": (2.0, 5.0)}.items():
        timeline_service.create_project(store, name)
        timeline_service.add_clip(store, media, name, "gig.mp4", in_point=a, out_point=b)
    timeline_service.create_project(store, "orphan")
    timeline_service.add_clip(store, media, "orphan", "other.mp4", in_point=0.0, out_point=2.0)
    (tmp_path / "other.mp4").unlink()
    return tmp_path


def _check_segments(ws):
    folder = ws / "gig_split"
    assert sorted(p.name for p in folder.glob("*.mp4")) == sorted(["01 - gig.mp4", "the-song.mp4"])
    for p in folder.glob("*.mp4"):
        assert probe_media(p).duration == pytest.approx(4.0, abs=0.5)


def _check_shorts(ws):
    for name in ("short1", "short2"):
        info = probe_media(ws / f"{name}.mp4")
        assert (info.width, info.height) == (1080, 1920) and info.has_audio
    assert probe_media(ws / "short2.mp4").duration == pytest.approx(3.0, abs=0.3)
    assert not (ws / "orphan.mp4").exists()


def _poll(client, batch_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/compose/render-queue/{batch_id}").json()
        if status["finished"]:
            return status
        time.sleep(0.1)
    raise AssertionError("export did not finish")


def _check_highlight_status(status):
    assert status["counts"] == {"queued": 0, "running": 0, "done": 2, "failed": 1, "cancelled": 0}
    states = [j["state"] for j in status["jobs"]]
    assert states == ["done", "done", "failed"]
    assert status["jobs"][0]["result"]["captions"]["style"] == "shorts"
    assert status["jobs"][0]["result"]["captions"]["words"] > 0
    error = status["jobs"][2]["error"]
    assert error["code"] == "source.not_found" and error["message"] and error["fix"]


HIGHLIGHTS = {"mode": "highlights", "timelines": ["short1", "short2", "orphan"]}


def test_http_all_three_modes(ws):
    with TestClient(create_app(ws, DEFAULTS, render_concurrency=2)) as client:
        started = client.post("/api/v1/compose/export", json={"mode": "segments", "video": "gig.mp4"})
        assert started.status_code == 200, started.text
        assert started.json()["mode"] == "segments" and started.json()["total"] == 2
        assert _poll(client, started.json()["batch_id"])["counts"]["done"] == 2
        _check_segments(ws)

        whole = client.post("/api/v1/compose/export", json={"mode": "timeline", "project": "short2"}).json()
        assert [j["output"] for j in whole["jobs"]] == ["short2.mp4"]
        assert _poll(client, whole["batch_id"])["counts"]["done"] == 1
        (ws / "short2.mp4").unlink()

        batch = client.post("/api/v1/compose/export", json=HIGHLIGHTS).json()
        assert batch["mode"] == "highlights" and batch["concurrency"] == 2
        _check_highlight_status(_poll(client, batch["batch_id"]))
        _check_shorts(ws)

        # Request errors are 422 with the catalogue code; an unlisted highlight is simply not exported.
        bad = client.post("/api/v1/compose/export", json={"mode": "bogus"})
        assert bad.status_code == 422 and bad.json()["code"] == "export.invalid"
        assert client.post("/api/v1/compose/export", json={"mode": "highlights"}).status_code == 422
        assert client.post("/api/v1/compose/export", json={"mode": "segments", "video": "nope.mp4"}).status_code == 404
        assert client.post("/api/v1/compose/export", json={"mode": "segments", "video": "../x.mp4"}).status_code in (
            403,
            404,
            422,
        )


def test_cli_modes_share_the_api_shape(ws):
    base = ["compose", "export", "--root", str(ws), "--json"]
    seg = RUNNER.invoke(app, [*base, "segments", "--video", "gig.mp4"])
    assert seg.exit_code == 0, seg.output
    status = json.loads(seg.output)
    assert status["mode"] == "segments" and status["counts"]["done"] == 2 and status["finished"] is True
    _check_segments(ws)
    whole = RUNNER.invoke(app, [*base, "timeline", "--project", "short1", "--output-dir", "out"])
    assert whole.exit_code == 0, whole.output
    assert (ws / "out" / "short1.mp4").exists()
    result = RUNNER.invoke(app, [*base, "highlights", "--timelines", "short1,short2,orphan"])
    assert result.exit_code == 1, result.output  # one short failed
    _check_highlight_status(json.loads(result.output))
    _check_shorts(ws)
    bad = RUNNER.invoke(app, ["compose", "export", "--root", str(ws), "highlights"])
    assert bad.exit_code == 1 and "timelines" in bad.output


async def test_mcp_modes(ws):
    jobs = InProcessJobQueue(max_workers=3, kind_limits={"render": 2})
    async with Client(create_server(ws, jobs=jobs)) as mcp:
        assert "export_project" in {t.name for t in (await mcp.list_tools()).tools}

        async def finish(args):
            started = await mcp.call_tool("export_project", args)
            assert not started.is_error, started.content
            status = started.structured_content
            deadline = time.monotonic() + 120
            while not status["finished"] and time.monotonic() < deadline:
                await anyio.sleep(0.1)
                batch = {"batch_id": status["batch_id"]}
                status = (await mcp.call_tool("render_queue_status", batch)).structured_content
            return started.structured_content["mode"], status

        mode, status = await finish({"mode": "segments", "video": "gig.mp4"})
        assert mode == "segments" and status["counts"]["done"] == 2
        _check_segments(ws)
        mode, status = await finish({"mode": "highlights", "timelines": ["short1", "short2", "orphan"]})
        _check_highlight_status(status)
        _check_shorts(ws)
        mode, status = await finish({"mode": "timeline", "video": "gig.mp4", "output": "gig-cut.mp4"})
        assert status["counts"]["done"] == 1 and probe_media(ws / "gig-cut.mp4").duration == pytest.approx(
            12.0, abs=0.5
        )
        bad = await mcp.call_tool("export_project", {"mode": "nope"})
        assert bad.is_error and "export.invalid" in bad.content[0].text
