"""The render queue over HTTP, the CLI and MCP with real ffmpeg (SPL-54): three renders queue and finish, a failing one
does not stop its siblings, and cancelling leaves no partial files. Output is read back with ffprobe."""

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest
from fake_transcript import document_from_words
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from split_video import assets, render, timeline_service, transcribe
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.ffmpeg import ProbeError, probe_media
from split_video.jobs import InProcessJobQueue
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.mcp_server import create_server

pytestmark = pytest.mark.anyio

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
RUNNER = CliRunner()
WORDS = [(0.5 + 0.4 * i, 0.5 + 0.4 * i + 0.3, w) for i, w in enumerate(["one", "two", "three", "four", "five.", "six"])]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ffmpeg(*args):
    result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    path = tmp_path_factory.mktemp("rq-source") / "talk.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    )  # fmt: skip
    return path


@pytest.fixture
def ws(source, tmp_path):
    """A workspace with the source, its transcript and three timelines (`reel`, `short1`, `short2`)."""
    shutil.copy(source, tmp_path / "talk.mp4")
    media, store = LocalMediaStore(tmp_path), LocalProjectStore(tmp_path)
    asset = assets.register_path(media, "talk.mp4")
    transcribe.save(media, asset, document_from_words(asset.id, WORDS))
    for name, (a, b) in {"reel": (0.0, 3.0), "short1": (1.0, 3.0), "short2": (2.0, 4.0)}.items():
        timeline_service.create_project(store, name)
        timeline_service.add_clip(store, media, name, "talk.mp4", in_point=a, out_point=b)
    return tmp_path


BATCH = [
    {"project": "reel", "preset": "youtube-1080p"},
    {"project": "short1", "preset": "vertical-1080", "fit": "crop", "captions": "shorts", "output": "short1.mp4"},
    {"project": "short2", "preset": "vertical-1080", "output": "short2.mp4"},
    {"project": "missing-timeline", "output": "bad.mp4"},
]


def _size(path):
    info = probe_media(path)
    return info.width, info.height


def _check_outputs(ws):
    assert _size(ws / "reel-render.mp4") == (1920, 1080)
    assert _size(ws / "short1.mp4") == (1080, 1920) and _size(ws / "short2.mp4") == (1080, 1920)
    assert probe_media(ws / "short1.mp4").duration == pytest.approx(2.0, abs=0.2)
    assert not (ws / "bad.mp4").exists()


def _poll(client, batch_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/compose/render-queue/{batch_id}").json()
        if status["finished"]:
            return status
        time.sleep(0.1)
    raise AssertionError("batch did not finish")


def _check_done_shape(status):
    states = [j["state"] for j in status["jobs"]]
    assert states == ["done", "done", "done", "failed"]
    assert status["counts"] == {"queued": 0, "running": 0, "done": 3, "failed": 1, "cancelled": 0}
    assert status["jobs"][1]["result"]["captions"]["style"] == "shorts"
    assert status["jobs"][1]["result"]["width"] == 1080
    assert status["jobs"][3]["error"]["code"] == "timeline.not_found" and status["jobs"][3]["result"] is None


def test_http_three_renders_finish_and_the_failing_one_does_not_stop_them(ws):
    with TestClient(create_app(ws, DEFAULTS, render_concurrency=2)) as client:
        started = client.post("/api/v1/compose/render-queue", json={"renders": BATCH})
        assert started.status_code == 200, started.text
        batch = started.json()
        assert batch["total"] == 4 and batch["concurrency"] == 2
        assert batch["jobs"][3]["state"] == "failed"  # rejected up front, in place
        status = _poll(client, batch["batch_id"])
        _check_done_shape(status)
        assert [j["job_id"] for j in status["jobs"]] == [j["job_id"] for j in batch["jobs"]]
        # The same jobs through the generic job API, grouped by the batch.
        job = client.get(f"/api/v1/jobs/{status['jobs'][0]['job_id']}").json()
        assert job["kind"] == "render" and job["group"] == batch["batch_id"] and job["state"] == "done"
        overview = client.get("/api/v1/compose/render-queue").json()
        assert overview["batch_id"] is None and overview["total"] == 4
    _check_outputs(ws)


def test_http_errors(ws):
    with TestClient(create_app(ws, DEFAULTS)) as client:
        assert client.post("/api/v1/compose/render-queue", json={"renders": []}).status_code == 422
        assert client.post("/api/v1/compose/render-queue", json={"renders": [{"preset": "x"}]}).status_code == 422
        assert client.get("/api/v1/compose/render-queue/nope").status_code == 404
        assert client.post("/api/v1/compose/render-queue/nope/cancel").status_code == 404


def test_http_cancel_a_running_and_a_queued_render(ws, monkeypatch):
    release = threading.Event()
    started = []
    real = render._run_ffmpeg

    def slow(plan, inputs, out_path, total, progress, cancel=None, preset=None, stall_timeout=None):
        started.append(out_path)
        Path(out_path).write_bytes(b"partial")
        while not release.is_set():
            cancel.raise_if_cancelled()
            time.sleep(0.02)
        return real(plan, inputs, out_path, total, progress, cancel, preset, stall_timeout)

    monkeypatch.setattr(render, "_run_ffmpeg", slow)
    with TestClient(create_app(ws, DEFAULTS, render_concurrency=1)) as client:
        batch = client.post("/api/v1/compose/render-queue", json={"renders": BATCH[:3]}).json()
        deadline = time.monotonic() + 10
        while not started and time.monotonic() < deadline:
            time.sleep(0.02)
        first, second, third = (j["job_id"] for j in batch["jobs"])
        assert [j["state"] for j in client.get(f"/api/v1/compose/render-queue/{batch['batch_id']}").json()["jobs"]] == [
            "running",
            "queued",
            "queued",
        ]
        # A queued render, cancelled on its own, never starts.
        assert client.post(f"/api/v1/jobs/{second}/cancel").json()["state"] == "cancelled"
        # The running one is killed and its partial file removed; the cancel frees the slot for the third.
        assert client.post(f"/api/v1/jobs/{first}/cancel").json()["state"] == "cancelled"
        assert not (ws / "reel-render.mp4").exists()
        deadline = time.monotonic() + 10
        while len(started) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(started) == 2 and started[1].endswith("short2.mp4")
        release.set()
        status = _poll(client, batch["batch_id"])
        assert [j["state"] for j in status["jobs"]] == ["cancelled", "cancelled", "done"]
        # Cancelling the whole batch afterwards leaves the finished one alone.
        again = client.post(f"/api/v1/compose/render-queue/{batch['batch_id']}/cancel").json()
        assert [j["state"] for j in again["jobs"]] == ["cancelled", "cancelled", "done"]
    assert not (ws / "short1.mp4").exists() and (ws / "short2.mp4").exists()


def test_http_cancel_the_whole_batch(ws, monkeypatch):
    started = []

    def slow(plan, inputs, out_path, total, progress, cancel=None, preset=None, stall_timeout=None):
        started.append(out_path)
        Path(out_path).write_bytes(b"partial")
        while True:
            cancel.raise_if_cancelled()
            time.sleep(0.02)

    monkeypatch.setattr(render, "_run_ffmpeg", slow)
    with TestClient(create_app(ws, DEFAULTS, render_concurrency=2)) as client:
        batch = client.post("/api/v1/compose/render-queue", json={"renders": BATCH[:3]}).json()
        deadline = time.monotonic() + 10
        while len(started) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        cancelled = client.post(f"/api/v1/compose/render-queue/{batch['batch_id']}/cancel").json()
        assert [j["state"] for j in cancelled["jobs"]] == ["cancelled"] * 3 and cancelled["finished"]
    assert len(started) == 2
    assert sorted(p.name for p in ws.glob("*.mp4")) == ["talk.mp4"]


def test_the_default_queue_caps_renders_from_the_environment(ws, monkeypatch):
    monkeypatch.setenv("SPLIT_VIDEO_RENDER_CONCURRENCY", "1")
    with TestClient(create_app(ws, DEFAULTS)) as client:
        batch = client.post("/api/v1/compose/render-queue", json={"renders": BATCH[:1]}).json()
        assert batch["concurrency"] == 1
        _poll(client, batch["batch_id"])
    monkeypatch.setenv("SPLIT_VIDEO_RENDER_CONCURRENCY", "99")
    with pytest.raises(ValueError, match="between 1 and"):
        create_app(ws, DEFAULTS)


def test_cli_render_queue_matches_the_api_shape(ws):
    spec = ws / "batch.json"
    spec.write_text(json.dumps({"renders": BATCH}))
    result = RUNNER.invoke(
        app, ["compose", "render-queue", str(spec), "--root", str(ws), "--concurrency", "2", "--json"]
    )
    assert result.exit_code == 1, result.output  # one render failed
    status = json.loads(result.output)
    _check_done_shape(status)
    assert status["concurrency"] == 2 and status["finished"] is True
    _check_outputs(ws)
    # A bad list is an error, not a traceback.
    spec.write_text("{}")
    bad = RUNNER.invoke(app, ["compose", "render-queue", str(spec), "--root", str(ws)])
    assert bad.exit_code == 1 and "list" in bad.output
    spec.write_text("[")
    assert RUNNER.invoke(app, ["compose", "render-queue", str(spec), "--root", str(ws)]).exit_code == 1


def test_cli_all_good_exits_zero_and_prints_a_summary(ws):
    spec = ws / "batch.json"
    spec.write_text(json.dumps(BATCH[:2]))
    result = RUNNER.invoke(app, ["compose", "render-queue", str(spec), "--root", str(ws)])
    assert result.exit_code == 0, result.output
    assert "2 done, 0 failed, 0 cancelled" in result.output


async def test_mcp_queue_status_and_cancel(ws):
    jobs = InProcessJobQueue(max_workers=2, kind_limits={"render": 2})
    async with Client(create_server(ws, jobs=jobs)) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert {"queue_renders", "render_queue_status", "cancel_renders"} <= names
        queued = await mcp.call_tool("queue_renders", {"renders": BATCH})
        assert not queued.is_error, queued.content
        batch = queued.structured_content
        assert batch["total"] == 4 and batch["jobs"][3]["state"] == "failed"
        status = batch
        deadline = time.monotonic() + 120
        while not status["finished"] and time.monotonic() < deadline:
            await anyio.sleep(0.1)
            status = (await mcp.call_tool("render_queue_status", {"batch_id": batch["batch_id"]})).structured_content
        _check_done_shape(status)
        everything = (await mcp.call_tool("render_queue_status", {})).structured_content
        assert everything["total"] == 4 and everything["batch_id"] is None
        unknown = await mcp.call_tool("render_queue_status", {"batch_id": "nope"})
        assert unknown.is_error and "unknown render batch" in unknown.content[0].text
        both = await mcp.call_tool("cancel_renders", {})
        assert both.is_error and "exactly one" in both.content[0].text
        again = await mcp.call_tool("cancel_renders", {"batch_id": batch["batch_id"]})
        assert again.structured_content["counts"]["done"] == 3
    jobs.shutdown()
    _check_outputs(ws)


async def test_mcp_over_a_real_stdio_subprocess(ws):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(ws), "--render-concurrency", "1"],
    )
    async with Client(params) as mcp:
        batch = (await mcp.call_tool("queue_renders", {"renders": BATCH[:2]})).structured_content
        assert batch["concurrency"] == 1
        status = batch
        deadline = time.monotonic() + 120
        while not status["finished"] and time.monotonic() < deadline:
            await anyio.sleep(0.1)
            status = (await mcp.call_tool("render_queue_status", {"batch_id": batch["batch_id"]})).structured_content
        assert [j["state"] for j in status["jobs"]] == ["done", "done"]
    assert _size(ws / "short1.mp4") == (1080, 1920)


def test_unreadable_output_is_detected_by_probe(ws):
    """Guards the helper the other tests lean on: ffprobe really rejects a bad file."""
    (ws / "junk.mp4").write_bytes(b"not video")
    with pytest.raises(ProbeError):
        probe_media(ws / "junk.mp4")


def test_http_on_the_in_memory_stores(source):
    """The same batch against in-memory project and media stores (the hosted-ready seam), no files on disk."""
    from split_video.media_store import InMemoryMediaStore
    from split_video.store import InMemoryProjectStore

    media, store = InMemoryMediaStore(materialise=True), InMemoryProjectStore()
    media.add("talk.mp4", source.read_bytes(), info=probe_media(source))
    for name in ("reel", "short1"):
        timeline_service.create_project(store, name)
        timeline_service.add_clip(store, media, name, "talk.mp4", in_point=0.0, out_point=2.0)
    with TestClient(create_app(source.parent, DEFAULTS, store=store, media=media)) as client:
        renders = [
            {"project": "reel", "preset": "draft-720"},
            {"project": "short1", "preset": "vertical-1080", "output": "s.mp4"},
            {"project": "ghost", "output": "g.mp4"},
        ]
        batch = client.post("/api/v1/compose/render-queue", json={"renders": renders}).json()
        status = _poll(client, batch["batch_id"])
    assert [j["state"] for j in status["jobs"]] == ["done", "done", "failed"]
    assert media.exists("reel-render.mp4") and media.exists("s.mp4") and not media.exists("g.mp4")
