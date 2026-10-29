"""What the export dialog (SPL-118) sends and reads: it exports labelled main-track clips by number, names the files
after their labels, shows `included` from the timeline and cancels a render for real. Real ffmpeg on a tiny file."""

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)


@pytest.fixture
def client(tmp_path, three_songs_clip):
    root = tmp_path / "ws"
    root.mkdir()
    shutil.copy(three_songs_clip, root / "gig.mp4")
    client = TestClient(create_app(root, DEFAULTS))
    client.root = root  # type: ignore[attr-defined]
    return client


def _wait_batch(client, batch_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/v1/compose/render-queue/{batch_id}").json()
        if status["finished"]:
            return status
        time.sleep(0.1)
    raise AssertionError("batch did not finish")


def test_timeline_clips_say_whether_they_are_included_and_export_by_label(client):
    duration = client.post("/api/v1/open", json={"path": "gig.mp4"}).json()["duration"]
    saved = client.post(
        "/api/v1/project",
        params={"path": "gig.mp4"},
        json={
            "segments": [
                {"start": 0, "end": 1, "label": "Song 1"},
                {"start": 1, "end": 2, "label": "Song 2", "included": False},
                {"start": 2, "end": min(3, duration), "label": "Song 3"},
            ]
        },
    )
    assert saved.status_code == 200
    clips = client.get("/api/v1/project/timeline", params={"path": "gig.mp4"}).json()["tracks"][0]["clips"]
    assert [c["included"] for c in clips] == [True, False, True]

    # The dialog sends the numbers of the included segments, a preset, a folder next to the recording.
    started = client.post(
        "/api/v1/compose/export",
        json={
            "mode": "segments",
            "video": "gig.mp4",
            "only": [1, 3],
            "preset": "draft-720",
            "fit": "letterbox",
            "output_dir": "gig_export",
            "overwrite": False,
        },
    )
    assert started.status_code == 200, started.text
    done = _wait_batch(client, started.json()["batch_id"])
    assert done["counts"]["done"] == 2
    assert sorted(p.name for p in (client.root / "gig_export").glob("*.mp4")) == ["Song 1.mp4", "Song 3.mp4"]

    # The same request again fails per file with a fix, which the dialog shows on the row.
    again = client.post(
        "/api/v1/compose/export",
        json={"mode": "segments", "video": "gig.mp4", "only": [1], "output_dir": "gig_export", "preset": "draft-720"},
    ).json()
    failed = again["jobs"][0]
    assert failed["state"] == "failed" and failed["error"]["code"] == "render.output_exists"
    assert failed["error"]["fix"]
    # Jobs of the queue are listed by kind, which is how the dialog reloads its rows.
    listed = client.get("/api/v1/jobs", params={"kind": "render"}).json()["jobs"]
    assert {j["id"] for j in listed} >= {j["job_id"] for j in done["jobs"]}
    assert all(j["kind"] == "render" for j in listed)


def test_a_queued_render_cancels_before_it_starts(client):
    client.post("/api/v1/open", json={"path": "gig.mp4"})
    client.post(
        "/api/v1/project",
        params={"path": "gig.mp4"},
        json={"segments": [{"start": i, "end": i + 1, "label": f"S{i}"} for i in range(6)]},
    )
    batch = client.post(
        "/api/v1/compose/export",
        json={"mode": "segments", "video": "gig.mp4", "only": [1, 2, 3, 4, 5, 6], "preset": "youtube-1080p"},
    ).json()
    last = batch["jobs"][-1]["job_id"]
    cancelled = client.post(f"/api/v1/jobs/{last}/cancel").json()
    assert cancelled["state"] == "cancelled"
    done = _wait_batch(client, batch["batch_id"], timeout=120)
    assert done["jobs"][-1]["state"] == "cancelled"
    assert not list(client.root.glob("S5*.mp4"))
    assert done["counts"]["failed"] == 0
