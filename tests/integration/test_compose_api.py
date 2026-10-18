import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)


def _make(path, seconds, color):
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:size=320x240:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path),
    ]  # fmt: skip
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


@pytest.fixture
def client(tmp_path):
    _make(tmp_path / "a.mp4", 4, "red")
    _make(tmp_path / "b.mp4", 3, "blue")
    return TestClient(create_app(tmp_path, DEFAULTS))


def _base(name="show"):
    return f"/api/compose/projects/{name}"


def _new(client, name="show"):
    assert client.post("/api/compose/projects", json={"name": name}).status_code == 200


def test_create_list_get_and_duplicate(client):
    _new(client)
    assert client.post("/api/compose/projects", json={"name": "show"}).status_code == 409
    assert client.get("/api/compose/projects").json() == {"projects": ["show"]}
    body = client.get(_base()).json()
    assert body["duration"] == 0 and body["tracks"][0]["kind"] == "video"
    assert client.get(_base("nope")).status_code == 404
    assert client.post("/api/compose/projects", json={"name": "../x"}).status_code == 422


def test_add_trim_move_transition_remove(client):
    _new(client)
    client.post(f"{_base()}/clips", json={"path": "a.mp4"})
    body = client.post(f"{_base()}/clips", json={"path": "b.mp4"}).json()
    a, b = body["tracks"][0]["clips"]
    assert a["source_duration"] == pytest.approx(4.0, abs=0.2) and b["start"] == pytest.approx(a["end"])

    body = client.patch(f"{_base()}/clips/{a['id']}", json={"in_point": 1.0}).json()
    assert body["tracks"][0]["clips"][0]["duration"] == pytest.approx(a["duration"] - 1.0)

    cid = b["id"]
    body = client.put(f"{_base()}/clips/{cid}/transition", json={"transition": "crossfade", "duration": 1.0}).json()
    assert body["tracks"][0]["clips"][1]["transition"] == "crossfade"

    body = client.post(f"{_base()}/clips/{cid}/move", json={"to_index": 0}).json()
    assert body["tracks"][0]["clips"][0]["id"] == cid

    body = client.delete(f"{_base()}/clips/{cid}").json()
    assert len(body["tracks"][0]["clips"]) == 1
    # persisted
    assert len(client.get(_base()).json()["tracks"][0]["clips"]) == 1


def test_errors_map_to_4xx(client):
    _new(client)
    assert client.post(f"{_base()}/clips", json={"path": "missing.mp4"}).status_code == 404
    assert client.post(f"{_base()}/clips", json={"path": "../a.mp4"}).status_code == 400
    assert client.post(f"{_base()}/clips", json={"path": "a.mp4", "out_point": 99}).status_code == 422
    assert client.post(f"{_base()}/clips", json={"path": "a.mp4", "track_id": "zzz"}).status_code == 422
    assert client.patch(f"{_base()}/clips/zzz", json={"in_point": 1}).status_code == 422
    assert client.post(f"{_base('nope')}/clips", json={"path": "a.mp4"}).status_code == 404


def test_render_job_reports_progress_and_finishes(client, tmp_path):
    _new(client)
    assert client.post(f"{_base()}/render", json={}).status_code == 422  # empty
    client.post(f"{_base()}/clips", json={"path": "a.mp4"})
    client.post(f"{_base()}/clips", json={"path": "b.mp4"})
    job = client.post(f"{_base()}/render", json={}).json()["job_id"]
    for _ in range(100):
        status = client.get(f"/api/compose/render/{job}").json()
        if status["status"] != "running":
            break
        time.sleep(0.2)
    assert status["status"] == "done" and status["progress"] == 1.0
    assert (tmp_path / status["output"]).is_file()
    assert client.post(f"{_base()}/render", json={}).status_code == 409
    assert client.post(f"{_base()}/render", json={"overwrite": True}).status_code == 200
    assert client.post(f"{_base()}/render", json={"output": "../x.mp4"}).status_code == 422
    assert client.get("/api/compose/render/nope").status_code == 404


def test_media_file_is_confined_to_root(client):
    assert client.get("/media/file", params={"path": "a.mp4"}).status_code == 200
    assert client.get("/media/file", params={"path": "../etc/passwd"}).status_code == 400
    assert client.get("/media/file", params={"path": "zzz.mp4"}).status_code == 404


def test_compose_page_is_served_and_linked_from_the_split_editor(client):
    assert 'id="compose-main"' in client.get("/compose.html").text
    assert "compose.html" in client.get("/").text
