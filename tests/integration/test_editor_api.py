import time

from fastapi.testclient import TestClient

import split_video.editor.cache as cache_module
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)


def _client(source):
    return TestClient(create_app(source, DEFAULTS))


def test_state_returns_initial_segments(three_songs_clip):
    client = _client(three_songs_clip)
    response = client.get("/api/state")
    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == three_songs_clip.name
    assert body["duration"] > 20.0
    assert len(body["segments"]) == 3
    assert body["params"]["silence_threshold"] == -35.0


def test_detect_only_calls_ffmpeg_once_per_distinct_threshold(three_songs_clip, monkeypatch):
    call_count = {"n": 0}
    real_detect_silence = cache_module.detect_silence

    def counting_detect_silence(*args, **kwargs):
        call_count["n"] += 1
        return real_detect_silence(*args, **kwargs)

    monkeypatch.setattr(cache_module, "detect_silence", counting_detect_silence)

    client = _client(three_songs_clip)
    assert call_count["n"] == 1  # the one initial detect at app startup

    for _ in range(3):
        response = client.post("/api/detect", json={"silence_threshold": -35.0})
        assert response.status_code == 200
    assert call_count["n"] == 1  # repeats with the same threshold are cache hits

    response = client.post("/api/detect", json={"silence_threshold": -40.0})
    assert response.status_code == 200
    assert call_count["n"] == 2  # a new threshold is the only thing that re-invokes ffmpeg


def test_segments_endpoint_is_pure_and_never_touches_ffmpeg(three_songs_clip, monkeypatch):
    client = _client(three_songs_clip)
    silences_response = client.get("/api/state").json()["silences"]

    def fail_if_called(*args, **kwargs):
        raise AssertionError("detect_silence should not be called by /api/segments")

    monkeypatch.setattr(cache_module, "detect_silence", fail_if_called)

    response = client.post(
        "/api/segments",
        json={
            "silences": silences_response,
            "duration": 21.0,
            "min_silence_duration": 2.0,
            "min_song_length": 2.0,
            "padding": 0.15,
        },
    )
    assert response.status_code == 200
    assert len(response.json()["segments"]) == 3


def test_export_writes_files_and_manifest(three_songs_clip):
    client = _client(three_songs_clip)
    segments = client.get("/api/state").json()["segments"]

    response = client.post(
        "/api/export",
        json={"segments": [{"start": s["start"], "end": s["end"]} for s in segments]},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    status = _poll_until_done(client, job_id)
    assert status["status"] == "done"
    assert status["completed"] == status["total"] == 3
    assert status["manifest_path"] is not None

    output_dir = three_songs_clip.with_name("three_songs_split")
    assert len(list(output_dir.glob("*.mp4"))) == 3


def test_export_rejects_overlapping_segments(three_songs_clip):
    client = _client(three_songs_clip)
    response = client.post(
        "/api/export",
        json={"segments": [{"start": 0.0, "end": 10.0}, {"start": 5.0, "end": 15.0}]},
    )
    assert response.status_code == 422


def test_media_source_supports_range_requests(three_songs_clip):
    client = _client(three_songs_clip)
    response = client.get("/media/source", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert "Content-Range" in response.headers
    assert len(response.content) == 100


def test_session_reports_file_open_for_file_source(three_songs_clip):
    client = _client(three_songs_clip)
    response = client.get("/api/session")
    assert response.status_code == 200
    assert response.json() == {"file_open": True, "filename": three_songs_clip.name}


def test_directory_source_starts_with_no_file_open(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    response = client.get("/api/session")
    assert response.status_code == 200
    assert response.json() == {"file_open": False, "filename": None}


def test_state_before_open_is_409(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    response = client.get("/api/state")
    assert response.status_code == 409


def test_browse_lists_video_in_directory_root(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    response = client.get("/api/browse")
    assert response.status_code == 200
    body = response.json()
    assert body["cwd"] == ""
    assert body["parent"] is None
    assert {e["name"] for e in body["entries"]} == {three_songs_clip.name}
    assert body["entries"][0]["is_dir"] is False


def test_browse_rejects_path_escaping_root(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    response = client.get("/api/browse", params={"path": "../"})
    assert response.status_code == 400


def test_open_then_state_matches_direct_file_mode(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    open_response = client.post("/api/open", json={"path": three_songs_clip.name})
    assert open_response.status_code == 200
    assert open_response.json()["filename"] == three_songs_clip.name

    session = client.get("/api/session").json()
    assert session == {"file_open": True, "filename": three_songs_clip.name}

    state = client.get("/api/state").json()
    assert state == open_response.json()


def test_open_rejects_nonexistent_file(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    response = client.post("/api/open", json={"path": "does-not-exist.mp4"})
    assert response.status_code == 404


def test_open_rejects_path_escaping_root(three_songs_clip, tmp_path):
    outside = tmp_path.parent / "outside.mp4"
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    response = client.post("/api/open", json={"path": f"../{outside.name}"})
    assert response.status_code == 400


def _poll_until_done(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/export/{job_id}").json()
        if status["status"] in ("done", "error"):
            return status
        time.sleep(0.05)
    raise AssertionError("export job did not finish in time")
