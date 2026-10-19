"""Library scan through the CLI, the HTTP API (as a job) and MCP, with real ffprobe on a tiny clip."""

import json
import shutil
import time

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from split_video import library
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()


@pytest.fixture
def workspace(tmp_path, three_songs_clip):
    root = tmp_path / "ws"
    (root / "shows").mkdir(parents=True)
    shutil.move(three_songs_clip, root / "shows" / "gig.mp4")
    (root / "broken.mp4").write_bytes(b"this is not a video")
    (root / "notes.txt").write_text("hi")
    return root


def _cli_json(*args):
    result = runner.invoke(app, ["library", *[str(a) for a in args], "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_cli_scan_then_list(workspace):
    report = _cli_json("scan", workspace)
    assert report["total"] == 2 and [a["path"] for a in report["added"]] == ["shows/gig.mp4"]
    assert report["skipped"][0]["path"] == "broken.mp4" and report["skipped"][0]["reason"]
    assert (workspace / ".split-video-assets.json").is_file()  # the sidecar next to the media

    (row,) = _cli_json("list", workspace)["assets"]
    assert row["path"] == "shows/gig.mp4" and row["has_video"] and row["has_audio"]
    assert (row["width"], row["height"]) == (320, 240) and row["duration"] == pytest.approx(21, abs=0.5)
    assert row["fps"] and row["present"]

    again = _cli_json("scan", workspace)
    assert again["unchanged"] == 1 and again["added"] == []


def test_cli_human_output_and_errors(workspace):
    out = runner.invoke(app, ["library", "scan", str(workspace)]).output
    assert "1 added" in out and "skipped" in out and "broken.mp4" in out
    assert "gig.mp4" in runner.invoke(app, ["library", "list", str(workspace)]).output
    bad = runner.invoke(app, ["library", "scan", str(workspace), "--folder", "nope"])
    assert bad.exit_code == 1


def test_cli_list_on_an_unscanned_folder_is_empty(workspace):
    assert _cli_json("list", workspace) == {"assets": []}


def _wait(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError("scan job did not finish")


def test_api_scan_runs_as_a_job_with_file_progress(workspace):
    client = TestClient(create_app(workspace, DEFAULTS))
    job_id = client.post("/api/v1/library/scan", json={}).json()["job_id"]
    job = _wait(client, job_id)
    assert job["kind"] == "library_scan" and job["state"] == "done"
    assert job["completed"] == job["total"] == 2 and job["fraction"] == 1.0
    assert [a["path"] for a in job["result"]["added"]] == ["shows/gig.mp4"]
    assert job["result"]["skipped"][0]["path"] == "broken.mp4"

    assets = client.get("/api/v1/assets").json()["assets"]
    assert [a["path"] for a in assets] == ["shows/gig.mp4"] and assets[0]["has_video"] is True

    # Scan again, scoped to a folder: nothing is probed again.
    job = _wait(client, client.post("/api/v1/library/scan", json={"folder": "shows"}).json()["job_id"])
    assert job["result"]["unchanged"] == 1 and job["result"]["added"] == []


def test_api_rejects_bad_folders_and_has_no_body_required(workspace):
    client = TestClient(create_app(workspace, DEFAULTS))
    assert client.post("/api/v1/library/scan", json={"folder": "nope"}).status_code == 404
    assert client.post("/api/v1/library/scan", json={"folder": "../x"}).status_code == 400
    assert client.post("/api/v1/library/scan").status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_scan_library_then_list_assets(workspace, anyio_backend):
    async with Client(create_server(workspace)) as client:
        scan = (await client.call_tool("scan_library", {})).structured_content
        assert [a["path"] for a in scan["added"]] == ["shows/gig.mp4"] and scan["skipped"][0]["path"] == "broken.mp4"
        listed = (await client.call_tool("list_assets", {})).structured_content["assets"]
        assert [a["path"] for a in listed] == ["shows/gig.mp4"] and listed[0]["has_video"]
        again = (await client.call_tool("scan_library", {"folder": "shows"})).structured_content
        assert again["unchanged"] == 1
        bad = await client.call_tool("scan_library", {"folder": "../x"})
        assert bad.is_error


def test_local_store_scan_probes_only_new_files(workspace, monkeypatch):
    media = LocalMediaStore(workspace.resolve())
    calls = []
    original = LocalMediaStore.probe
    monkeypatch.setattr(LocalMediaStore, "probe", lambda self, mid: (calls.append(mid), original(self, mid))[1])
    library.scan(media)
    assert calls == ["broken.mp4", "shows/gig.mp4"] or sorted(calls) == ["broken.mp4", "shows/gig.mp4"]
    calls.clear()
    library.scan(media)
    assert calls == ["broken.mp4"]  # only the unreadable file is retried; the recorded one is not
