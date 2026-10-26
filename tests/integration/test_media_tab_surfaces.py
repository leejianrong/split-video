"""What the Media tab needs from the server: importing a file by upload and the `split` job, with real ffmpeg."""

import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from split_video import assets
from split_video.cli import app as cli_app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def incoming(tmp_path, three_songs_clip):
    """The clip in a folder that is not the workspace, as a file on the user's computer."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    return shutil.copy(three_songs_clip, folder / "gig night.mp4")


def _wait(client, job_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _upload(client, path, name, folder=""):
    with open(path, "rb") as handle:
        return client.post(
            "/api/v1/assets/upload",
            params={"name": name, "folder": folder},
            content=handle.read(),
            headers={"Content-Type": "application/octet-stream"},
        )


def test_upload_writes_the_file_into_the_workspace_and_registers_it(workspace, incoming):
    client = TestClient(create_app(workspace, DEFAULTS))
    response = _upload(client, incoming, "gig night.mp4")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["path"] == "gig night.mp4" and body["has_video"] and body["present"]
    assert body["duration"] == pytest.approx(21, abs=1)
    assert (workspace / "gig night.mp4").stat().st_size == Path(incoming).stat().st_size
    listed = client.get("/api/v1/assets").json()["assets"]
    assert [a["id"] for a in listed] == [body["id"]]
    # it is also in the sidecar asset table next to the media
    assert json.loads((workspace / ".split-video-assets.json").read_text())["assets"][body["id"]]


def test_upload_never_overwrites_and_drops_folders_from_the_name(workspace, incoming):
    (workspace / "shows").mkdir()
    client = TestClient(create_app(workspace, DEFAULTS))
    first = _upload(client, incoming, "../../gig.mp4", folder="shows").json()
    second = _upload(client, incoming, "gig.mp4", folder="shows").json()
    assert first["path"] == "shows/gig.mp4"
    # the same bytes are the same asset; the second file is stored under a free name and listed as a copy
    assert first["id"] == second["id"]
    assert second["path"] == "shows/gig.mp4" and second["copies"] == ["shows/gig (2).mp4"]
    assert (workspace / "shows" / "gig (2).mp4").is_file()


def test_upload_errors_carry_a_code_and_a_fix_and_leave_nothing_on_disk(workspace, tmp_path):
    client = TestClient(create_app(workspace, DEFAULTS))
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"this is not a video")

    wrong_type = _upload(client, junk, "notes.txt")
    assert wrong_type.status_code == 415
    assert wrong_type.json()["code"] == "upload.unsupported_type" and wrong_type.json()["fix"]

    unreadable = _upload(client, junk, "broken.mp4")
    assert unreadable.status_code == 422
    assert unreadable.json()["code"] == "media.unreadable" and unreadable.json()["fix"]

    empty = client.post("/api/v1/assets/upload", params={"name": "empty.mp4"}, content=b"")
    assert empty.status_code == 400 and empty.json()["code"] == "upload.empty"

    outside = _upload(client, junk, "x.mp4", folder="../elsewhere")
    assert outside.status_code == 400

    missing = _upload(client, junk, "x.mp4", folder="nope")
    assert missing.status_code in (400, 404)
    assert [p.name for p in workspace.iterdir() if not p.name.startswith(".")] == []


def test_split_job_saves_labelled_segments_and_reports_progress(workspace, three_songs_clip):
    shutil.copy(three_songs_clip, workspace / "gig.mp4")
    client = TestClient(create_app(workspace, DEFAULTS))
    asset = client.post("/api/v1/assets/register", json={"path": "gig.mp4"}).json()

    started = client.post(f"/api/v1/assets/{asset['id']}/split", json={"label_prefix": "Song", "min_song_length": 2.0})
    assert started.status_code == 200
    job = _wait(client, started.json()["job_id"])
    assert job["kind"] == "split" and job["state"] == "done", job
    assert job["result"]["segments"] == 3
    assert job["result"]["labels"] == ["Song 1", "Song 2", "Song 3"]
    assert job["completed"] == job["total"] or job["total"] == 1000

    state = client.post("/api/v1/open", json={"path": "gig.mp4"}).json()
    assert [s["label"] for s in state["segments"]] == ["Song 1", "Song 2", "Song 3"]
    assert state["revision"] == job["result"]["revision"]
    timeline = client.get("/api/v1/project/timeline", params={"path": "gig.mp4"}).json()
    assert [c["label"] for c in timeline["tracks"][0]["clips"]] == ["Song 1", "Song 2", "Song 3"]


def test_split_job_without_labels_and_with_a_stale_revision(workspace, three_songs_clip):
    shutil.copy(three_songs_clip, workspace / "gig.mp4")
    client = TestClient(create_app(workspace, DEFAULTS))
    asset = client.post("/api/v1/assets/register", json={"path": "gig.mp4"}).json()
    url = f"/api/v1/assets/{asset['id']}/split"

    plain = _wait(client, client.post(url, json={"min_song_length": 2.0}).json()["job_id"])
    assert plain["state"] == "done" and plain["result"]["labels"] == ["", "", ""]
    current = plain["result"]["revision"]

    stale = _wait(
        client,
        client.post(url, json={"min_song_length": 2.0}, headers={"If-Match": f'"{current - 1}"'}).json()["job_id"],
    )
    assert stale["state"] == "error"
    assert stale["error_info"]["code"] == "revision.conflict" and stale["error_info"]["fix"]

    fresh = _wait(
        client, client.post(url, json={"min_song_length": 2.0}, headers={"If-Match": f'"{current}"'}).json()["job_id"]
    )
    assert fresh["state"] == "done" and fresh["result"]["revision"] == current + 1


def test_split_job_rejects_unknown_assets_and_audio_less_files(workspace, tmp_path):
    import subprocess

    client = TestClient(create_app(workspace, DEFAULTS))
    assert client.post("/api/v1/assets/a_0000000000000000/split").status_code == 404
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=64x64:duration=2",
            str(workspace / "mute.mp4"),
        ],
        capture_output=True,
        check=True,
    )
    asset = client.post("/api/v1/assets/register", json={"path": "mute.mp4"}).json()
    assert client.post(f"/api/v1/assets/{asset['id']}/split").status_code == 422


def test_cli_propose_label_prefix_matches_the_api(workspace, three_songs_clip):
    shutil.copy(three_songs_clip, workspace / "gig.mp4")
    result = CliRunner().invoke(
        cli_app,
        ["propose", str(workspace / "gig.mp4"), "--min-song-length", "2", "--label-prefix", "Song", "--json"],
    )
    assert result.exit_code == 0, result.output
    labels = [s["label"] for s in json.loads(result.output)["segments"]]
    assert labels == ["Song 1", "Song 2", "Song 3"]
    media = LocalMediaStore(workspace.resolve())
    assert assets.list_assets(media) == []  # a proposal alone registers nothing


def _two_videos(workspace, three_songs_clip, two_songs_clip):
    (workspace / "shows").mkdir()
    shutil.copy(three_songs_clip, workspace / "shows" / "gig.mp4")
    shutil.copy(two_songs_clip, workspace / "intro.mp4")


def test_video_timeline_is_the_recording_as_one_clip_until_saved(workspace, three_songs_clip, two_songs_clip):
    _two_videos(workspace, three_songs_clip, two_songs_clip)
    client = TestClient(create_app(workspace, DEFAULTS))
    timeline = client.get("/api/v1/project/timeline", params={"path": "shows/gig.mp4"})
    assert timeline.status_code == 200, timeline.text
    body = timeline.json()
    main = body["tracks"][0]
    assert body["revision"] == 0 and body["name"] == "shows/gig.mp4"
    assert [(c["source"], c["in_point"]) for c in main["clips"]] == [("shows/gig.mp4", 0.0)]
    assert not (workspace / "shows" / "gig.mp4.split-video-timeline.json").exists()  # reading wrote nothing
    assert client.get("/api/v1/project/timeline", params={"path": "nope.mp4"}).status_code == 404
    assert client.get("/api/v1/project/timeline", params={"path": "../x.mp4"}).status_code == 400


def test_adding_an_asset_appends_a_clip_with_if_match(workspace, three_songs_clip, two_songs_clip):
    _two_videos(workspace, three_songs_clip, two_songs_clip)
    client = TestClient(create_app(workspace, DEFAULTS))
    intro = client.post("/api/v1/assets/register", json={"path": "intro.mp4"}).json()
    url, params = "/api/v1/project/clips", {"path": "shows/gig.mp4"}

    added = client.post(url, params=params, json={"asset": intro["id"]}, headers={"If-Match": '"0"'})
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["revision"] == 1
    assert [c["source"] for c in body["tracks"][0]["clips"]] == ["shows/gig.mp4", "intro.mp4"]
    assert body["tracks"][0]["clips"][1]["asset"] == intro["id"]
    assert (workspace / "shows" / "gig.mp4.split-video-timeline.json").exists()  # the sidecar, next to the video

    stale = client.post(url, params=params, json={"asset": intro["id"]}, headers={"If-Match": '"0"'})
    assert stale.status_code == 409 and stale.json()["error"] == "revision_conflict"
    assert stale.json()["current_revision"] == 1

    first = client.post(url, params=params, json={"asset": intro["id"], "index": 0}, headers={"If-Match": '"1"'}).json()
    assert [c["source"] for c in first["tracks"][0]["clips"]] == ["intro.mp4", "shows/gig.mp4", "intro.mp4"]
    after = client.get("/api/v1/project/timeline", params=params)
    assert after.status_code == 200, after.text
    assert after.json()["revision"] == 2
    # the open project's own state reports the same revision, so the editor's mirror stays consistent
    assert client.post("/api/v1/open", json={"path": "shows/gig.mp4"}).json()["revision"] == 2


def test_adding_refuses_unknown_audio_only_and_out_of_range(workspace, three_songs_clip, two_songs_clip):
    import subprocess

    _two_videos(workspace, three_songs_clip, two_songs_clip)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=duration=2", str(workspace / "song.mp3")],
        capture_output=True,
        check=True,
    )
    client = TestClient(create_app(workspace, DEFAULTS))
    audio = client.post("/api/v1/assets/register", json={"path": "song.mp3"}).json()
    intro = client.post("/api/v1/assets/register", json={"path": "intro.mp4"}).json()
    params = {"path": "shows/gig.mp4"}
    unknown = client.post("/api/v1/project/clips", params=params, json={"asset": "a_0000000000000000"})
    assert unknown.status_code == 404 and unknown.json()["code"] == "asset.not_found"
    only_audio = client.post("/api/v1/project/clips", params=params, json={"asset": audio["id"]})
    assert only_audio.status_code == 422 and only_audio.json()["code"] == "media.audio_only"
    far = client.post("/api/v1/project/clips", params=params, json={"asset": intro["id"], "index": 9})
    assert far.status_code == 422
    assert client.get("/api/v1/project/timeline", params=params).json()["revision"] == 0  # nothing was written


def test_a_split_after_adding_another_recording_says_to_use_the_compose_editor(
    workspace, three_songs_clip, two_songs_clip
):
    _two_videos(workspace, three_songs_clip, two_songs_clip)
    client = TestClient(create_app(workspace, DEFAULTS))
    gig = client.post("/api/v1/assets/register", json={"path": "shows/gig.mp4"}).json()
    intro = client.post("/api/v1/assets/register", json={"path": "intro.mp4"}).json()
    client.post("/api/v1/project/clips", params={"path": "shows/gig.mp4"}, json={"asset": intro["id"]})
    job = _wait(
        client, client.post(f"/api/v1/assets/{gig['id']}/split", json={"min_song_length": 2.0}).json()["job_id"]
    )
    assert job["state"] == "error"
    assert "compose editor" in job["error"]
    assert job["error_info"]["code"] == "segments.main_track_edited" and "compose" in job["error_info"]["fix"]
