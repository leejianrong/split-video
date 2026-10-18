"""Export, analysis and render run as jobs on the queue: polling shapes, the generic /jobs
routes, and cancellation through HTTP (with a real ffmpeg for render)."""

import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from split_video import project as project_module
from split_video.editor import classify as classify_module
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.ffmpeg import run_cancellable
from split_video.jobs import InProcessJobQueue

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)


def _q(source):
    return {"path": source.name}


def _wait(client, job_id, until=("done", "error", "cancelled"), timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in until:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} stayed {job['state']}")


def _segments(three_songs_clip, n=3):
    return [{"start": i * 5.0, "end": i * 5.0 + 4.0} for i in range(n)]


# --- export -------------------------------------------------------------------------------------


def test_export_job_is_queryable_by_id_after_it_finishes(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    job_id = client.post(
        "/api/v1/export", params=_q(three_songs_clip), json={"segments": _segments(three_songs_clip)}
    ).json()["job_id"]
    job = _wait(client, job_id)
    assert job["state"] == "done" and job["kind"] == "export" and job["label"] == three_songs_clip.name
    assert job["completed"] == job["total"] == 3 and job["fraction"] == 1.0
    assert len(job["result"]["files"]) == 3 and job["result"]["manifest_path"].endswith("manifest.json")
    assert job["created_at"] <= job["started_at"] <= job["finished_at"]

    legacy = client.get(f"/api/v1/export/{job_id}").json()
    assert set(legacy) == {
        "status",
        "completed",
        "total",
        "current_file",
        "output_dir",
        "manifest_path",
        "files",
        "error",
    }
    assert legacy["status"] == "done" and legacy["files"] == job["result"]["files"] and legacy["error"] is None

    listed = client.get("/api/v1/jobs", params={"kind": "export"}).json()["jobs"]
    assert [j["id"] for j in listed] == [job_id]
    assert client.get("/api/v1/jobs", params={"state": "error"}).json() == {"jobs": []}


def test_job_ids_are_only_valid_on_their_own_kinds_endpoints(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    job_id = client.post(
        "/api/v1/export", params=_q(three_songs_clip), json={"segments": _segments(three_songs_clip, 1)}
    ).json()["job_id"]
    _wait(client, job_id)
    assert client.get(f"/api/v1/analyze/{job_id}").status_code == 404
    assert client.get(f"/api/v1/compose/render/{job_id}").status_code == 404
    assert client.post(f"/api/v1/compose/render/{job_id}/cancel").status_code == 404
    assert client.get("/api/v1/jobs/nope").status_code == 404
    assert client.post("/api/v1/jobs/nope/cancel").status_code == 404
    assert client.get(f"/api/v1/export/{job_id}").status_code == 200


def test_cancelling_a_finished_job_returns_its_final_state(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    job_id = client.post(
        "/api/v1/export", params=_q(three_songs_clip), json={"segments": _segments(three_songs_clip, 1)}
    ).json()["job_id"]
    _wait(client, job_id)
    again = client.post(f"/api/v1/jobs/{job_id}/cancel").json()
    assert again["state"] == "done" and again["cancel_requested"] is False


def test_export_error_is_reported_in_the_polling_shape(three_songs_clip, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(project_module, "extract_segment", broken)
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    job_id = client.post(
        "/api/v1/export", params=_q(three_songs_clip), json={"segments": _segments(three_songs_clip, 1)}
    ).json()["job_id"]
    job = _wait(client, job_id)
    legacy = client.get(f"/api/v1/export/{job_id}").json()
    assert job["state"] == "error" and legacy["status"] == "error" and legacy["error"] == "disk full"
    assert legacy["output_dir"] is None and legacy["files"] == []


def test_cancelling_an_export_keeps_finished_files_and_removes_the_partial_one(three_songs_clip, monkeypatch):
    calls = []

    def fake_extract(source, start, end, out_path, precise, output_format=None, cancel=None):
        calls.append(out_path.name)
        out_path.write_bytes(b"x")
        if len(calls) == 2:
            run_cancellable([sys.executable, "-c", "import time; time.sleep(60)"], cancel)

    monkeypatch.setattr(project_module, "extract_segment", fake_extract)
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    job_id = client.post(
        "/api/v1/export", params=_q(three_songs_clip), json={"segments": _segments(three_songs_clip)}
    ).json()["job_id"]
    while len(calls) < 2:
        time.sleep(0.01)
    time.sleep(0.2)  # the second file's sleeper is up
    response = client.post(f"/api/v1/export/{job_id}/cancel").json()
    assert response["status"] == "cancelled" and response["error"] == "cancelled"
    out = three_songs_clip.with_name("three_songs_split")
    assert sorted(p.name for p in out.iterdir()) == [calls[0]]  # no 2nd file, no 3rd, no manifest
    assert len(calls) == 2
    assert client.get(f"/api/v1/jobs/{job_id}").json()["result"]["files"] == [str(out / calls[0])]


# --- analysis -----------------------------------------------------------------------------------


def test_analysis_error_when_the_model_is_missing(three_songs_clip, monkeypatch):
    def missing():
        raise classify_module.ModelNotFoundError("no model; run make fetch-model")

    monkeypatch.setattr(classify_module, "get_model_and_labels", missing)
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    job_id = client.post("/api/v1/analyze", params=_q(three_songs_clip)).json()["job_id"]
    _wait(client, job_id)
    status = client.get(f"/api/v1/analyze/{job_id}").json()
    assert status == {
        "status": "error",
        "completed": 0,
        "total": status["total"],
        "error": "no model; run make fetch-model",
    }


def test_already_analyzed_is_a_job_that_is_done_on_submit(three_songs_clip, monkeypatch):
    monkeypatch.setattr(classify_module, "get_model_and_labels", lambda: (object(), object()))
    monkeypatch.setattr(classify_module, "classify_audio", lambda *a, **k: ([], [{"music": 0.1}], 1.0))
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS, jobs=InProcessJobQueue(synchronous=True)))
    first = client.post("/api/v1/analyze", params=_q(three_songs_clip)).json()["job_id"]
    assert client.get(f"/api/v1/jobs/{first}").json()["state"] == "done"  # synchronous queue: finished at submit
    second = client.post("/api/v1/analyze", params=_q(three_songs_clip)).json()["job_id"]
    job = client.get(f"/api/v1/jobs/{second}").json()
    assert job["state"] == "done" and job["kind"] == "analysis" and job["started_at"] is not None
    assert client.get(f"/api/v1/analyze/{second}").json() == {
        "status": "done",
        "completed": 0,
        "total": 0,
        "error": None,
    }


def test_cancelling_an_analysis_stops_between_frames_and_stores_nothing(three_songs_clip, monkeypatch):
    frames = []

    def slow_classify(pcm, model, labels, thresholds, on_progress=None):
        for i in range(100000):
            frames.append(i)
            on_progress(i + 1, 100000)
            time.sleep(0.002)
        raise AssertionError("was never cancelled")

    monkeypatch.setattr(classify_module, "get_model_and_labels", lambda: (object(), object()))
    monkeypatch.setattr(classify_module, "classify_audio", slow_classify)
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    job_id = client.post("/api/v1/analyze", params=_q(three_songs_clip)).json()["job_id"]
    while len(frames) < 5:
        time.sleep(0.01)
    status = client.post(f"/api/v1/analyze/{job_id}/cancel").json()
    assert status["status"] == "cancelled" and status["total"] == 100000
    seen = len(frames)
    time.sleep(0.05)
    assert len(frames) == seen  # the loop really stopped
    assert client.get("/api/v1/classification", params=_q(three_songs_clip)).json()["analyzed"] is False


# --- render -------------------------------------------------------------------------------------


def _make(path, seconds):
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=red:size=320x240:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path),
    ]  # fmt: skip
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


@pytest.fixture
def realtime_ffmpeg(monkeypatch):
    """Make every render's ffmpeg read its input at 1x (`-re`) so a short clip takes long enough to
    cancel mid-way, and record the processes so a test can check none is left running."""
    procs = []
    real_popen = subprocess.Popen

    def popen(cmd, *args, **kwargs):
        if "-progress" in cmd:
            cmd = [cmd[0], "-re", *cmd[1:]]
        proc = real_popen(cmd, *args, **kwargs)
        if "-progress" in cmd:
            procs.append(proc)
        return proc

    monkeypatch.setattr("split_video.render.subprocess.Popen", popen)
    return procs


def _start_render(client, tmp_path, name="show"):
    _make(tmp_path / "long.mp4", 30)
    base = f"/api/v1/compose/projects/{name}"
    assert client.post("/api/v1/compose/projects", json={"name": name}).status_code == 200
    assert client.post(f"{base}/clips", json={"path": "long.mp4"}).status_code == 200
    return client.post(f"{base}/render", json={"output": "out.mp4"}).json()["job_id"]


@pytest.mark.parametrize("route", ["/api/v1/compose/render/{id}/cancel", "/api/v1/jobs/{id}/cancel"])
def test_cancelling_a_render_kills_ffmpeg_and_deletes_the_partial_output(tmp_path, realtime_ffmpeg, route):
    client = TestClient(create_app(tmp_path, DEFAULTS))
    job_id = _start_render(client, tmp_path)
    running = _wait(client, job_id, until=("running",))
    while client.get(f"/api/v1/jobs/{job_id}").json()["completed"] == 0:  # ffmpeg is encoding
        time.sleep(0.02)
    assert running["kind"] == "render" and len(realtime_ffmpeg) == 1
    ffmpeg = realtime_ffmpeg[0]
    assert ffmpeg.poll() is None and (tmp_path / "out.mp4").exists()

    response = client.post(route.format(id=job_id))
    assert response.status_code == 200

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["state"] == "cancelled" and job["error"] is None and job["finished_at"] is not None
    assert ffmpeg.poll() is not None  # terminated and reaped
    assert subprocess.run(["pgrep", "-f", str(tmp_path / "out.mp4")], capture_output=True, check=False).returncode == 1
    assert not (tmp_path / "out.mp4").exists()
    assert 0.0 < job["fraction"] < 1.0

    status = client.get(f"/api/v1/compose/render/{job_id}").json()
    assert status["status"] == "cancelled" and status["error"] == "cancelled" and status["output"] is None
    assert client.post(route.format(id=job_id)).status_code == 200  # idempotent
    # The output name is free again: a fresh render does not hit the "already exists" conflict.
    assert client.post("/api/v1/compose/projects/show/render", json={"output": "out.mp4"}).status_code == 200


def test_render_through_the_queue_keeps_the_polling_shape(tmp_path):
    _make(tmp_path / "a.mp4", 2)
    client = TestClient(create_app(tmp_path, DEFAULTS))
    client.post("/api/v1/compose/projects", json={"name": "show"})
    client.post("/api/v1/compose/projects/show/clips", json={"path": "a.mp4"})
    job_id = client.post("/api/v1/compose/projects/show/render", json={}).json()["job_id"]
    job = _wait(client, job_id)
    status = client.get(f"/api/v1/compose/render/{job_id}").json()
    assert set(status) == {"status", "progress", "output", "error"}
    assert status == {"status": "done", "progress": 1.0, "output": "show-render.mp4", "error": None}
    assert job["kind"] == "render" and job["result"] == {"output": "show-render.mp4"}
    assert (tmp_path / "show-render.mp4").is_file()


def test_render_error_is_reported_and_leaves_the_job_queryable(tmp_path):
    _make(tmp_path / "a.mp4", 2)
    client = TestClient(create_app(tmp_path, DEFAULTS))
    client.post("/api/v1/compose/projects", json={"name": "show"})
    client.post("/api/v1/compose/projects/show/clips", json={"path": "a.mp4"})
    (tmp_path / "a.mp4").unlink()
    job_id = client.post("/api/v1/compose/projects/show/render", json={}).json()["job_id"]
    job = _wait(client, job_id)
    status = client.get(f"/api/v1/compose/render/{job_id}").json()
    assert job["state"] == "error" and status["status"] == "error" and "not found" in status["error"]
