"""What the timeline lanes need from the server (SPL-65): edit ops on a recording's own timeline and the waveform."""

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
    return TestClient(create_app(root, DEFAULTS))


def _wait(client, job_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _split(client):
    asset = client.post("/api/v1/assets/register", json={"path": "gig.mp4"}).json()
    job = client.post(f"/api/v1/assets/{asset['id']}/split", json={"label_prefix": "Song", "min_song_length": 2.0})
    assert _wait(client, job.json()["job_id"])["state"] == "done"
    return asset


def _timeline(client):
    return client.get("/api/v1/project/timeline", params={"path": "gig.mp4"}).json()


def _ops(client, ops, revision, **params):
    return client.post(
        "/api/v1/project/ops",
        params={"path": "gig.mp4", **params},
        json={"schema_version": 1, "ops": ops},
        headers={"If-Match": f'"{revision}"'},
    )


def test_trim_and_move_apply_engine_ops_and_bump_the_revision(client):
    _split(client)
    before = _timeline(client)
    clips = before["tracks"][0]["clips"]
    assert [c["label"] for c in clips] == ["Song 1", "Song 2", "Song 3"]
    first = clips[0]

    trimmed = _ops(
        client, [{"op": "trim_clip", "clip": first["id"], "out": first["out_point"] - 1.0}], before["revision"]
    )
    assert trimmed.status_code == 200, trimmed.text
    body = trimmed.json()
    assert body["revision"] == before["revision"] + 1 and body["applied"] == 1 and body["project"] == "gig.mp4"
    assert body["results"][0]["summary"].startswith("trimmed clip")
    after = _timeline(client)
    assert after["revision"] == body["revision"]
    new = after["tracks"][0]["clips"]
    assert new[0]["duration"] == pytest.approx(first["duration"] - 1.0, abs=0.01)
    assert new[1]["start"] == pytest.approx(clips[1]["start"] - 1.0, abs=0.01)  # the magnetic track closes up

    moved = _ops(client, [{"op": "move_clip", "clip": clips[2]["id"], "to_index": 0}], after["revision"])
    assert moved.status_code == 200
    ids = [c["id"] for c in _timeline(client)["tracks"][0]["clips"]]
    assert ids[0] == clips[2]["id"]

    stale = _ops(client, [{"op": "trim_clip", "clip": first["id"], "out": 5}], before["revision"])
    assert stale.status_code == 409
    assert stale.json()["error"] == "revision_conflict" and stale.json()["current_revision"] == moved.json()["revision"]


def test_dry_run_writes_nothing_and_a_bad_op_is_a_422_report(client):
    _split(client)
    before = _timeline(client)
    clip = before["tracks"][0]["clips"][0]
    dry = _ops(client, [{"op": "trim_clip", "clip": clip["id"], "out": 2.5}], before["revision"], dry_run="true")
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    assert dry.json()["timeline"]["tracks"][0]["clips"][0]["out"] == 2.5
    assert _timeline(client)["revision"] == before["revision"]
    bad = _ops(client, [{"op": "trim_clip", "clip": "nope", "out": 2.5}], before["revision"])
    assert bad.status_code == 422 and bad.json()["errors"][0]["code"] == "ops.clip.not_found"
    mismatch = client.post(
        "/api/v1/project/ops",
        params={"path": "gig.mp4"},
        json={
            "schema_version": 1,
            "project": "other.mp4",
            "ops": [{"op": "set_track_flags", "track": "x", "muted": True}],
        },
    )
    assert mismatch.status_code == 422 and mismatch.json()["errors"][0]["code"] == "ops.project.mismatch"
    assert client.post("/api/v1/project/ops", params={"path": "missing.mp4"}, json={}).status_code == 404


def test_track_flags_are_in_the_timeline_and_a_locked_track_refuses_edits(client):
    _split(client)
    before = _timeline(client)
    track = before["tracks"][0]
    assert (track["locked"], track["hidden"], track["muted"]) == (False, False, False)
    locked = _ops(
        client, [{"op": "set_track_flags", "track": track["id"], "locked": True, "muted": True}], before["revision"]
    )
    assert locked.status_code == 200
    now = _timeline(client)
    assert (now["tracks"][0]["locked"], now["tracks"][0]["muted"]) == (True, True)
    refused = _ops(client, [{"op": "trim_clip", "clip": track["clips"][0]["id"], "out": 3}], now["revision"])
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["code"] == "ops.track.locked"
    assert _timeline(client)["revision"] == now["revision"]
    hide_main = _ops(client, [{"op": "set_track_flags", "track": track["id"], "hidden": True}], now["revision"])
    assert hide_main.json()["errors"][0]["code"] == "ops.track.main_hidden"


def test_waveform_is_404_until_the_job_has_run_then_serves_both_curves(client):
    asset = _split(client)
    url = f"/api/v1/assets/{asset['id']}/waveform"
    missing = client.get(url)
    assert missing.status_code == 404 and "POST" in missing.json()["detail"]
    job = _wait(client, client.post(url).json()["job_id"])
    assert job["kind"] == "waveform" and job["state"] == "done", job
    got = client.get(url).json()
    assert got["asset"] == asset["id"] and got["duration"] == pytest.approx(21, abs=1)
    assert len(got["peaks"]) > 100 and all(-1 <= lo <= hi <= 1 for lo, hi in got["peaks"])
    assert got["loudness_window"] == 1.0 and len(got["loudness"]) in (21, 22)
    assert client.get("/api/v1/assets/a_0000000000000000/waveform").status_code == 404
