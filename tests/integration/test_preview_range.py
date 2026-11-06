"""Preview range chunks through the HTTP API, the CLI and MCP, with real ffmpeg (SPL-70)."""

import json
import os
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from split_video import preview_range, render, timeline_service
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.jobs import JobCancelledError
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()
pytestmark = pytest.mark.filterwarnings("ignore")
RESULT_KEYS = {
    "project", "revision", "key", "media", "path", "mime", "start", "end", "duration", "width", "height", "fps",
    "source", "bytes", "cached",
}  # fmt: skip
URL = "/api/v1/compose/projects/show/preview-range"
BODY = {"start": 1.0, "end": 2.5, "height": 180}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def workspace(tmp_path):
    for name, colour in (("red", "red"), ("blue", "blue")):
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=25:d=3"]
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:d=3", "-shortest", "-pix_fmt", "yuv420p"]
        assert subprocess.run([*cmd, str(tmp_path / f"{name}.mp4")], check=False).returncode == 0
    store, media = LocalProjectStore(tmp_path.resolve()), LocalMediaStore(tmp_path.resolve())
    timeline_service.create_project(store, "show")
    timeline_service.add_clip(store, media, "show", "red.mp4")
    timeline_service.add_clip(store, media, "show", "blue.mp4")
    return tmp_path


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,profile,width,height:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout  # fmt: skip
    data = json.loads(out)
    return {s["codec_name"]: s for s in data["streams"]}, float(data["format"]["duration"])


def finish(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    raise AssertionError("the preview job did not finish")


def chunks(workspace):
    return sorted(p.name for p in (workspace / ".split-video-cache").glob("*preview-*.mp4"))


def test_api_renders_a_chunk_that_plays_and_serves_ranges(workspace):
    client = TestClient(create_app(workspace, DEFAULTS))
    started = client.post(URL, json=BODY).json()
    assert started["cached"] is False and started["result"] is None and started["revision"] == 2
    job = finish(client, started["job_id"])
    assert job["kind"] == "preview_range" and job["state"] == "done", job
    result = job["result"]
    assert set(result) == RESULT_KEYS and result["cached"] is False and result["key"] == started["key"]
    assert (result["start"], result["end"], result["duration"]) == (1.0, 2.5, 1.5)
    assert (result["width"], result["height"], result["source"], result["mime"]) == (320, 180, "original", "video/mp4")

    chunk = client.get(f"{URL}/{result['key']}")
    assert chunk.status_code == 200 and chunk.headers["content-type"] == "video/mp4"
    assert chunk.headers["accept-ranges"] == "bytes" and len(chunk.content) == result["bytes"]
    part = client.get(f"{URL}/{result['key']}", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == chunk.content[:100]
    assert part.headers["content-range"] == f"bytes 0-99/{result['bytes']}"

    (workspace / "got.mp4").write_bytes(chunk.content)
    streams, duration = probe(workspace / "got.mp4")  # H.264 constrained baseline and AAC: plays in every browser
    assert streams["h264"]["profile"] == "Constrained Baseline" and "aac" in streams
    assert (streams["h264"]["width"], streams["h264"]["height"]) == (320, 180)
    assert duration == pytest.approx(1.5, abs=0.1)
    assert chunks(workspace) != [] and (workspace / "red.mp4").exists()


def test_a_second_identical_request_renders_nothing_and_an_edit_makes_a_new_chunk(workspace, monkeypatch):
    client = TestClient(create_app(workspace, DEFAULTS))
    first = finish(client, client.post(URL, json=BODY).json()["job_id"])["result"]

    def no_ffmpeg(*args, **kwargs):
        raise AssertionError("a cache hit must not run ffmpeg")

    monkeypatch.setattr(render, "_run_ffmpeg", no_ffmpeg)
    t0 = time.perf_counter()
    again = client.post(URL, json=BODY).json()
    hit_ms = (time.perf_counter() - t0) * 1000
    assert again["cached"] is True and again["key"] == first["key"] and again["result"]["cached"] is True
    assert client.get(f"/api/v1/jobs/{again['job_id']}").json()["state"] == "done"
    assert hit_ms < 1000
    monkeypatch.undo()

    client.post("/api/v1/compose/projects/show/clips", json={"path": "red.mp4"})  # an edit: revision 3
    after = client.post(URL, json=BODY).json()
    assert after["cached"] is False and after["revision"] == 3 and after["key"] != first["key"]
    assert client.get(f"{URL}/{first['key']}").status_code == 404  # the old key is no longer served
    second = finish(client, after["job_id"])["result"]
    assert client.get(f"{URL}/{second['key']}").status_code == 200
    assert client.get(f"{URL}/{first['key']}").status_code == 404
    assert len(chunks(workspace)) == 1  # the old revision's file was deleted with the new one stored


def test_errors(workspace):
    client = TestClient(create_app(workspace, DEFAULTS))
    long = client.post(URL, json={"start": 0, "end": 31})
    assert long.status_code == 400 and long.json()["code"] == "preview.range_too_long"
    assert long.json()["details"] == {"max": 30.0, "requested": 31.0}
    assert client.post(URL, json={"start": 2, "end": 2}).status_code == 422
    assert client.post(URL, json={"start": 99, "end": 100}).status_code == 422
    assert client.post(URL, json={"start": -1, "end": 2}).status_code == 422
    assert client.post(URL, json={**BODY, "source": "proxy"}).status_code == 400
    assert client.post(URL, json={**BODY, "height": 5000}).status_code == 400
    assert client.post(URL, json={**BODY, "preset": "nope"}).status_code == 422
    assert client.post("/api/v1/compose/projects/nope/preview-range", json=BODY).status_code == 404
    assert client.get(f"{URL}/{'0' * 20}").status_code == 404
    past = client.post(URL, json={"start": 5, "end": 20}).json()  # cut to the end of the 6 s timeline
    assert finish(client, past["job_id"])["result"]["end"] == 6.0


def test_a_cancelled_render_leaves_no_file_and_no_cache_entry(workspace):
    store, media = LocalProjectStore(workspace.resolve()), LocalMediaStore(workspace.resolve())
    project = timeline_service.load_existing(store, "show", media)
    anchor = preview_range.anchor_of_project("show")
    plan = preview_range.plan_range(project, media, anchor, 0, 2, name="show", height=90)

    class Cancelled:
        def is_cancelled(self):
            return True

        def raise_if_cancelled(self):
            raise JobCancelledError()

        def on_cancel(self, callback):
            callback()  # already cancelled: ffmpeg is stopped at once
            return lambda: None

    with pytest.raises(JobCancelledError):
        preview_range.render_range(media, plan, cancel=Cancelled())
    assert chunks(workspace) == [] and preview_range.lookup(media, plan) is None


@pytest.fixture
def held_renders(monkeypatch):
    """Every render waits until its job is cancelled (or the test ends), so the abort race is driven by hand."""
    release = threading.Event()  # the test is over

    def work_for(media, plan):
        def work(ctx):
            cancelled = threading.Event()
            ctx.on_cancel(cancelled.set)
            while not (cancelled.wait(0.05) or release.is_set()):
                pass
            ctx.raise_if_cancelled()
            return {}

        return work

    monkeypatch.setattr("split_video.editor.preview_range.preview_range_work", work_for)
    yield
    release.set()


def live(client):
    return [
        j for j in client.get("/api/v1/jobs?kind=preview_range").json()["jobs"] if j["state"] in ("queued", "running")
    ]


def test_cancelling_by_key_ends_a_render_whose_job_id_was_never_read(workspace, held_renders):
    client = TestClient(create_app(workspace, DEFAULTS))
    headers = {"Idempotency-Key": "chunk-1"}
    first = client.post(URL, json=BODY, headers=headers).json()  # the browser aborted before reading this
    again = client.post(URL, json={**BODY, "end": 2.0}, headers=headers).json()
    assert again["job_id"] == first["job_id"]  # a retry with the same key is the same job
    other = client.post(URL, json=BODY, headers={"Idempotency-Key": "chunk-2"}).json()
    assert other["job_id"] != first["job_id"]

    assert client.delete("/api/v1/preview-range-requests/chunk-1").status_code == 204
    assert client.delete("/api/v1/preview-range-requests/chunk-1").status_code == 204  # idempotent
    assert finish(client, first["job_id"])["state"] == "cancelled"
    assert [j["id"] for j in live(client)] == [other["job_id"]]  # only the other key's render is left


def test_a_cancel_that_arrives_before_its_post_leaves_no_job_running(workspace, held_renders):
    client = TestClient(create_app(workspace, DEFAULTS))
    assert client.delete("/api/v1/preview-range-requests/late").status_code == 204
    late = client.post(URL, json=BODY, headers={"Idempotency-Key": "late"}).json()
    assert finish(client, late["job_id"])["state"] == "cancelled" and live(client) == []


def test_the_remembered_keys_are_bounded(workspace, held_renders):
    client = TestClient(create_app(workspace, DEFAULTS))
    for n in range(300):
        client.delete(f"/api/v1/preview-range-requests/k{n}")
    first = client.post(URL, json=BODY, headers={"Idempotency-Key": "k0"}).json()  # k0 was forgotten: a fresh job
    job = client.get(f"/api/v1/jobs/{first['job_id']}").json()
    assert job["cancel_requested"] is False and job["state"] in ("queued", "running")


def test_the_cache_keeps_a_bounded_number_of_chunks(workspace, monkeypatch):
    monkeypatch.setattr(preview_range, "KEEP", 2)
    store, media = LocalProjectStore(workspace.resolve()), LocalMediaStore(workspace.resolve())
    project = timeline_service.load_existing(store, "show", media)
    anchor = preview_range.anchor_of_project("show")
    plans = [preview_range.plan_range(project, media, anchor, n, n + 0.5, name="show", height=90) for n in (0, 1, 2)]
    preview_range.render_range(media, plans[0])
    preview_range.render_range(media, plans[1])
    assert preview_range.lookup(media, plans[0]) is not None  # used again: the second one is now the oldest
    preview_range.render_range(media, plans[2])
    assert preview_range.lookup(media, plans[1]) is None and not media.is_file(plans[1].chunk)
    assert preview_range.lookup(media, plans[0]) is not None and preview_range.lookup(media, plans[2]) is not None
    assert len(chunks(workspace)) == 2


def test_replacing_a_source_gives_a_fresh_chunk_for_the_same_revision_and_range(workspace):
    store, media = LocalProjectStore(workspace.resolve()), LocalMediaStore(workspace.resolve())
    project = timeline_service.load_existing(store, "show", media)
    anchor = preview_range.anchor_of_project("show")

    def plan():
        return preview_range.plan_range(project, media, anchor, 0, 0.5, name="show", height=90)

    first = plan()
    assert preview_range.render_range(media, first)["cached"] is False
    assert plan().key == first.key and preview_range.render_range(media, plan())["cached"] is True
    red = workspace / "red.mp4"
    stat = red.stat()
    os.utime(red, (stat.st_atime, stat.st_mtime + 10))  # same size, replaced a moment later
    second = plan()
    assert second.key != first.key and second.revision == first.revision
    assert preview_range.render_range(media, second)["cached"] is False


def test_the_cache_never_exceeds_the_byte_cap_and_drops_the_oldest_first(workspace, monkeypatch):
    store, media = LocalProjectStore(workspace.resolve()), LocalMediaStore(workspace.resolve())
    project = timeline_service.load_existing(store, "show", media)
    anchor = preview_range.anchor_of_project("show")
    plans = [preview_range.plan_range(project, media, anchor, n, n + 0.5, name="show", height=90) for n in (0, 1, 2)]
    size = preview_range.render_range(media, plans[0])["bytes"]
    monkeypatch.setattr(preview_range, "MAX_BYTES", size * 2 + size // 2, raising=False)  # room for about two chunks
    preview_range.render_range(media, plans[1])
    preview_range.render_range(media, plans[2])
    total = sum((workspace / ".split-video-cache" / n).stat().st_size for n in chunks(workspace))
    assert total <= size * 2 + size // 2
    assert preview_range.lookup(media, plans[0]) is None and not media.is_file(plans[0].chunk)
    assert preview_range.lookup(media, plans[2]) is not None


def test_a_range_starting_inside_a_cross_fade_is_exact_and_mixes_the_sound(workspace):
    store, media = LocalProjectStore(workspace.resolve()), LocalMediaStore(workspace.resolve())
    clip = timeline_service.load_existing(store, "show", media).tracks[0].clips[1]
    timeline_service.set_transition(store, "show", clip.id, "crossfade", 1.0, media)
    project = timeline_service.load_existing(store, "show", media)  # 5 s: the fade is 2..3 s
    plan = preview_range.plan_range(project, media, preview_range.anchor_of_project("show"), 2.5, 4.0, height=90)
    result = preview_range.render_range(media, plan)
    streams, duration = probe(result["path"])
    assert (result["start"], result["end"]) == (2.5, 4.0) and duration == pytest.approx(1.5, abs=0.1)
    assert "aac" in streams and "h264" in streams


def test_the_videos_own_timeline_has_the_same_routes(workspace):
    client = TestClient(create_app(workspace, DEFAULTS))
    url = "/api/v1/project/preview-range"
    started = client.post(url, params={"path": "red.mp4"}, json=BODY).json()
    result = finish(client, started["job_id"])["result"]
    assert set(result) == RESULT_KEYS and result["revision"] == 0 and result["project"] == "red.mp4"
    chunk = client.get(f"{url}/{result['key']}", params={"path": "red.mp4"})
    assert chunk.status_code == 200 and len(chunk.content) == result["bytes"]
    assert client.post(url, params={"path": "red.mp4"}, json=BODY).json()["cached"] is True
    assert client.get(f"{url}/{result['key']}", params={"path": "blue.mp4"}).status_code == 404
    assert client.post(url, params={"path": "nope.mp4"}, json=BODY).status_code == 404
    assert client.post(url, params={"path": "../x.mp4"}, json=BODY).status_code == 400


def test_cli_prints_the_same_json(workspace):
    r = runner.invoke(
        app, ["compose", "preview-range", "show", "1", "2.5", "--root", str(workspace), "--height", "180"]
    )
    assert r.exit_code == 0, r.output
    result = json.loads(r.stdout)
    assert set(result) == RESULT_KEYS and result["cached"] is False and result["revision"] == 2
    assert probe(result["path"])[1] == pytest.approx(1.5, abs=0.1)
    again = json.loads(
        runner.invoke(
            app, ["compose", "preview-range", "show", "1", "0:02.5", "--root", str(workspace), "--height", "180"]
        ).stdout
    )
    assert again["cached"] is True and again["key"] == result["key"]
    video = runner.invoke(
        app, ["compose", "preview-range", "--video", "red.mp4", "0", "1", "--root", str(workspace), "--height", "90"]
    )
    assert video.exit_code == 0 and set(json.loads(video.stdout)) == RESULT_KEYS
    long = runner.invoke(app, ["compose", "preview-range", "show", "0", "40", "--root", str(workspace)])
    assert long.exit_code == 1 and "at most 30 s" in long.output
    assert runner.invoke(app, ["compose", "preview-range", "show", "1", "--root", str(workspace)]).exit_code == 2


@pytest.mark.anyio
async def test_mcp_returns_a_reference_not_the_video(workspace):
    async with Client(create_server(workspace)) as client:
        assert "preview_range" in {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("preview_range", {"name": "show", "start": 1, "end": 2.5, "height": 180})
        assert not result.is_error, result.content
        info = result.structured_content
        assert set(info) == RESULT_KEYS and info["revision"] == 2 and info["cached"] is False
        assert len(json.dumps(info)) < 2000 and all(c.type == "text" for c in result.content)
        video = await client.call_tool("preview_range", {"video": "red.mp4", "start": 0, "end": 1, "height": 90})
        assert not video.is_error and video.structured_content["revision"] == 0
        long = await client.call_tool("preview_range", {"name": "show", "start": 0, "end": 31})
        assert long.is_error and "at most 30 s" in long.content[0].text
        both = await client.call_tool("preview_range", {"name": "show", "video": "red.mp4", "start": 0, "end": 1})
        assert both.is_error
