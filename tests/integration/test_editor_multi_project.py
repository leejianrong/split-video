"""Editor state is per project: two clients on one app never see each other's."""

import re
import time
from pathlib import Path

from fastapi.testclient import TestClient

from split_video.editor import classify as classify_module
from split_video.editor.app import create_app
from split_video.editor.project_store import _project_path as sidecar_path
from split_video.editor.schemas import StateParams

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)

BUCKETS = ("music", "singing", "speech", "applause_crowd", "laughter", "silence_other")


def _spans(segments):
    return [(s["start"], s["end"]) for s in segments]


def test_two_clients_edit_different_files_at_once(three_songs_clip, two_songs_clip, monkeypatch):
    app = create_app(three_songs_clip.parent, DEFAULTS)
    alice = TestClient(app)
    bob = TestClient(app)
    a = {"path": three_songs_clip.name}
    b = {"path": two_songs_clip.name}

    # Interleaved opens: neither open disturbs the other.
    state_a = alice.post("/api/v1/open", json=a).json()
    state_b = bob.post("/api/v1/open", json=b).json()
    assert state_a["filename"] == three_songs_clip.name
    assert state_b["filename"] == two_songs_clip.name
    assert state_a["duration"] > state_b["duration"]
    assert state_a["video_url"] != state_b["video_url"]

    # Interleaved saves of different split projects.
    splits_a = [{"start": 0.0, "end": 6.0, "label": "A1"}, {"start": 6.0, "end": 20.0, "label": "A2"}]
    splits_b = [{"start": 0.0, "end": 9.0, "label": "B1"}]
    assert alice.post("/api/v1/project", params=a, json={"segments": splits_a}).status_code == 200
    assert bob.post("/api/v1/project", params=b, json={"segments": splits_b}).status_code == 200

    got_a = alice.get("/api/v1/state", params=a).json()
    got_b = bob.get("/api/v1/state", params=b).json()
    assert _spans(got_a["segments"]) == [(0.0, 6.0), (6.0, 20.0)]
    assert [s["label"] for s in got_a["segments"]] == ["A1", "A2"]
    assert _spans(got_b["segments"]) == [(0.0, 9.0)]
    assert [s["label"] for s in got_b["segments"]] == ["B1"]
    # Each client reading the other's project sees that project's state only.
    assert _spans(bob.get("/api/v1/state", params=a).json()["segments"]) == [(0.0, 6.0), (6.0, 20.0)]

    # Sidecars on disk are separate, next to each video.
    assert sidecar_path(three_songs_clip).exists()
    assert sidecar_path(two_songs_clip).exists()
    assert "A1" in sidecar_path(three_songs_clip).read_text()
    assert "A1" not in sidecar_path(two_songs_clip).read_text()

    # Waveform caches are per project.
    wave_a = alice.get("/api/v1/waveform", params=a).json()["buckets"]
    wave_b = bob.get("/api/v1/waveform", params=b).json()["buckets"]
    assert wave_a and wave_b and wave_a != wave_b

    # Silence detection is per project too: different clips, different gaps.
    sil_a = alice.post("/api/v1/detect", params=a, json={"silence_threshold": -35.0}).json()["silences"]
    sil_b = bob.post("/api/v1/detect", params=b, json={"silence_threshold": -35.0}).json()["silences"]
    assert len(sil_a) == 2 and len(sil_b) == 1

    # Analysing A leaves B's classification cache untouched.
    monkeypatch.setattr(classify_module, "get_model_and_labels", lambda: (object(), object()))
    monkeypatch.setattr(
        classify_module, "classify_audio", lambda *args, **kwargs: ([], [dict.fromkeys(BUCKETS, 0.1)], 1.0)
    )
    job_id = alice.post("/api/v1/analyze", params=a).json()["job_id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        status = alice.get(f"/api/v1/analyze/{job_id}").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert status["status"] == "done", status.get("error")
    assert alice.get("/api/v1/classification", params=a).json()["analyzed"] is True
    assert bob.get("/api/v1/classification", params=b).json()["analyzed"] is False

    # Both videos stream independently.
    assert alice.get("/api/v1/media/source", params=a, headers={"Range": "bytes=0-9"}).status_code == 206
    size_a = int(
        alice.get("/api/v1/media/source", params=a, headers={"Range": "bytes=0-0"})
        .headers["content-range"]
        .split("/")[1]
    )
    size_b = int(
        bob.get("/api/v1/media/source", params=b, headers={"Range": "bytes=0-0"}).headers["content-range"].split("/")[1]
    )
    assert size_a == three_songs_clip.stat().st_size
    assert size_b == two_songs_clip.stat().st_size


def test_file_argument_is_only_a_default_not_a_current_file(three_songs_clip, two_songs_clip):
    app = create_app(three_songs_clip, DEFAULTS)
    client = TestClient(app)
    assert client.get("/api/v1/session").json()["path"] == three_songs_clip.name
    # Another project on the same server is still reachable and independent.
    other = client.get("/api/v1/state", params={"path": two_songs_clip.name}).json()
    assert other["filename"] == two_songs_clip.name
    assert (
        client.get("/api/v1/state", params={"path": three_songs_clip.name}).json()["filename"] == three_songs_clip.name
    )


def test_no_global_session_state_in_editor_package():
    editor_dir = Path(__file__).resolve().parents[2] / "src" / "split_video" / "editor"
    source = (editor_dir / "app.py").read_text()
    assert not re.search(r"class\s+_?Session\b", source)
    assert "_require_open" not in source
    assert "session.source" not in source
