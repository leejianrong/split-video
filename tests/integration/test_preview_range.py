"""Preview range chunks through the HTTP API, the CLI and MCP, with real ffmpeg (SPL-70)."""

import json
import subprocess
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
