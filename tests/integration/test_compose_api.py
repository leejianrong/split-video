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
    return f"/api/v1/compose/projects/{name}"


def _new(client, name="show"):
    assert client.post("/api/v1/compose/projects", json={"name": name}).status_code == 200


def test_create_list_get_and_duplicate(client):
    _new(client)
    assert client.post("/api/v1/compose/projects", json={"name": "show"}).status_code == 409
    assert client.get("/api/v1/compose/projects").json() == {"projects": ["show"]}
    body = client.get(_base()).json()
    assert body["duration"] == 0 and body["tracks"][0]["kind"] == "video"
    assert client.get(_base("nope")).status_code == 404
    assert client.post("/api/v1/compose/projects", json={"name": "../x"}).status_code == 422


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
        status = client.get(f"/api/v1/compose/render/{job}").json()
        if status["status"] != "running":
            break
        time.sleep(0.2)
    assert status["status"] == "done" and status["progress"] == 1.0
    assert (tmp_path / status["output"]).is_file()
    assert client.post(f"{_base()}/render", json={}).status_code == 409
    assert client.post(f"{_base()}/render", json={"overwrite": True}).status_code == 200
    assert client.post(f"{_base()}/render", json={"output": "../x.mp4"}).status_code == 422
    assert client.get("/api/v1/compose/render/nope").status_code == 404


def test_media_file_is_confined_to_root(client):
    assert client.get("/api/v1/media/file", params={"path": "a.mp4"}).status_code == 200
    assert client.get("/api/v1/media/file", params={"path": "../etc/passwd"}).status_code == 400
    assert client.get("/api/v1/media/file", params={"path": "zzz.mp4"}).status_code == 404


def test_compose_page_is_served_and_linked_from_the_split_editor(client):
    assert 'id="compose-main"' in client.get("/compose.html").text
    assert "compose.html" in client.get("/").text


def test_tracks_free_placement_and_stacking(client):
    _new(client)
    client.post(f"{_base()}/clips", json={"path": "a.mp4"})
    body = client.post(f"{_base()}/tracks", json={"kind": "video"}).json()
    main, overlay = body["tracks"]
    assert (main["magnetic"], overlay["magnetic"]) == (True, False)
    assert client.post(f"{_base()}/tracks", json={"kind": "text"}).status_code == 422

    body = client.post(f"{_base()}/clips", json={"path": "b.mp4", "track_id": overlay["id"], "start": 1.5}).json()
    clip = body["tracks"][1]["clips"][0]
    assert clip["start"] == 1.5 and clip["kind"] == "media" and clip["opacity"] == 1.0
    # index on a free track / start on the main track are mistakes
    assert (
        client.post(f"{_base()}/clips", json={"path": "b.mp4", "track_id": overlay["id"], "index": 0}).status_code
        == 422
    )
    assert client.post(f"{_base()}/clips", json={"path": "b.mp4", "start": 3}).status_code == 422
    # overlap is refused, moving in time and to another track works
    assert (
        client.post(f"{_base()}/clips", json={"path": "b.mp4", "track_id": overlay["id"], "start": 2}).status_code
        == 422
    )
    body = client.post(f"{_base()}/clips/{clip['id']}/place", json={"start": 0.5}).json()
    assert body["tracks"][1]["clips"][0]["start"] == 0.5
    body = client.post(f"{_base()}/tracks", json={"kind": "video"}).json()
    third = body["tracks"][2]["id"]
    body = client.post(f"{_base()}/clips/{clip['id']}/place", json={"track_id": third}).json()
    assert body["tracks"][2]["clips"][0]["id"] == clip["id"]

    body = client.post(f"{_base()}/tracks/{third}/move", json={"to_index": 1}).json()
    assert body["tracks"][1]["id"] == third
    body = client.delete(f"{_base()}/tracks/{third}").json()
    assert len(body["tracks"]) == 2
    assert client.delete(f"{_base()}/tracks/{main['id']}").status_code == 200  # the overlay is still a video track
    assert client.delete(f"{_base()}/tracks/{overlay['id']}").status_code == 422  # last video track


def test_audio_track_rejects_files_without_audio_and_video_track_rejects_audio_only(client, tmp_path):
    _new(client)
    body = client.post(f"{_base()}/tracks", json={"kind": "audio"}).json()
    audio = body["tracks"][1]["id"]
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=duration=2", str(tmp_path / "tone.wav")],
        capture_output=True, check=True,
    )  # fmt: skip
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:size=64x64:rate=25:duration=2", "-pix_fmt", "yuv420p",
         str(tmp_path / "silent.mp4")],
        capture_output=True, check=True,
    )  # fmt: skip
    assert client.post(f"{_base()}/clips", json={"path": "tone.wav", "track_id": audio, "start": 0}).status_code == 200
    assert client.post(f"{_base()}/clips", json={"path": "silent.mp4", "track_id": audio}).status_code == 422
    assert client.post(f"{_base()}/clips", json={"path": "tone.wav"}).status_code == 422  # audio-only onto video


def test_clip_properties_and_text_clips(client):
    _new(client)
    body = client.post(f"{_base()}/clips", json={"path": "a.mp4"}).json()
    clip = body["tracks"][0]["clips"][0]["id"]
    body = client.patch(
        f"{_base()}/clips/{clip}/properties", json={"gain": 1.5, "muted": True, "saturation": 0.4}
    ).json()
    got = body["tracks"][0]["clips"][0]
    assert (got["gain"], got["muted"], got["saturation"]) == (1.5, True, 0.4)
    assert client.patch(f"{_base()}/clips/{clip}/properties", json={"opacity": 3}).status_code == 422
    assert client.patch(f"{_base()}/clips/{clip}/properties", json={}).status_code == 422
    assert client.patch(f"{_base()}/clips/zzz/properties", json={"gain": 1}).status_code == 422

    overlay = client.post(f"{_base()}/tracks", json={"kind": "video"}).json()["tracks"][1]["id"]
    body = client.post(f"{_base()}/tracks/{overlay}/text", json={"text": "Hello", "start": 1, "duration": 2}).json()
    text = body["tracks"][1]["clips"][0]
    assert (text["kind"], text["text"], text["start"], text["duration"]) == ("text", "Hello", 1, 2)
    body = client.patch(f"{_base()}/clips/{text['id']}/text", json={"text": "Bye", "color": "#ff0000"}).json()
    assert (body["tracks"][1]["clips"][0]["text"], body["tracks"][1]["clips"][0]["color"]) == ("Bye", "#ff0000")
    assert client.patch(f"{_base()}/clips/{clip}/text", json={"text": "x"}).status_code == 422  # not a text clip
    assert client.post(f"{_base()}/tracks/{overlay}/text", json={"text": " "}).status_code == 422
    # trimming a text clip stretches it (no source to run out of)
    body = client.patch(f"{_base()}/clips/{text['id']}", json={"out_point": 6}).json()
    assert body["tracks"][1]["clips"][0]["duration"] == 6
