"""Scene and activity signals through the HTTP API (as jobs), the CLI and MCP, with real ffmpeg on a clip whose
cuts are at 5, 12 and 20 s (SPL-43)."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_scenes import CUTS, TOLERANCE, known_cuts_clip

from split_video import assets, scenes
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "streams").mkdir(parents=True)
    known_cuts_clip(root / "streams" / "coding.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=duration=2", str(root / "song.mp3")],
        capture_output=True,
        check=True,
    )
    return root


def _wait(client, job_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _coding(root):
    media = LocalMediaStore(root.resolve())
    return media, assets.register_path(media, "streams/coding.mp4")


def _near(found, expected=CUTS):
    return len(found) == len(expected) and all(abs(f - c) <= TOLERANCE for f, c in zip(found, expected, strict=True))


def test_api_scene_job_then_get_signals(workspace):
    _, asset = _coding(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    assert client.get(f"{base}/scenes").status_code == 404
    assert client.get(base).json()["scenes"] is False

    job = _wait(client, client.post(f"{base}/scenes").json()["job_id"])
    assert job["kind"] == "scene_analysis" and job["state"] == "done"
    assert job["completed"] == job["total"] == 1000
    assert job["result"]["made"] is True and job["result"]["scenes"] == 3 and job["result"]["source"] == "original"

    doc = client.get(f"{base}/scenes").json()
    assert doc["asset"] == asset.id and doc["version"] == 1 and doc["activity_interval"] == 1.0
    assert _near([s["time"] for s in doc["scenes"]]) and len(doc["activity"]) == 26 and "activity_start" not in doc
    cut = client.get(f"{base}/scenes", params={"start": 10, "end": 14}).json()
    assert [s["time"] for s in cut["scenes"]] == [12.0] and len(cut["activity"]) == 4 and cut["activity_start"] == 10.0
    assert client.get(base).json()["scenes"] is True
    assert [a["scenes"] for a in client.get("/api/v1/assets").json()["assets"] if a["has_video"]] == [True]

    assert _wait(client, client.post(f"{base}/scenes").json()["job_id"])["result"]["made"] is False
    loose = _wait(client, client.post(f"{base}/scenes", json={"threshold": 0.9}).json()["job_id"])
    assert loose["result"]["made"] is True and loose["result"]["scenes"] == 0


def test_api_errors(workspace):
    media, _ = _coding(workspace)
    song = assets.register_path(media, "song.mp3")
    client = TestClient(create_app(workspace, DEFAULTS))
    assert client.post(f"/api/v1/assets/{song.id}/scenes").status_code == 422
    assert client.post("/api/v1/assets/a_0000000000000000/scenes").status_code == 404
    assert client.get("/api/v1/assets/a_0000000000000000/scenes").status_code == 404
    assert client.get(f"/api/v1/assets/{song.id}/scenes").status_code == 404
    _, asset = _coding(workspace)
    assert client.post(f"/api/v1/assets/{asset.id}/scenes", json={"threshold": 2}).status_code == 422
    assert client.get("/api/v1/jobs").json()["jobs"] == []


def test_api_cancel_stops_a_running_analysis_and_stores_nothing(workspace, monkeypatch):
    media, asset = _coding(workspace)
    real = scenes.iter_stdout_lines
    monkeypatch.setattr(
        scenes, "iter_stdout_lines", lambda cmd, cancel=None, what="": real([cmd[0], "-re", *cmd[1:]], cancel, what)
    )
    client = TestClient(create_app(workspace, DEFAULTS))
    job_id = client.post(f"/api/v1/assets/{asset.id}/scenes").json()["job_id"]
    deadline = time.time() + 20
    while time.time() < deadline and client.get(f"/api/v1/jobs/{job_id}").json()["state"] != "running":
        time.sleep(0.02)
    time.sleep(0.5)
    started = time.time()
    client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert _wait(client, job_id)["state"] == "cancelled"
    assert time.time() - started < 10  # ffmpeg was stopped, not waited out (the clip is 26 s at real time)
    assert client.get(f"/api/v1/assets/{asset.id}/scenes").status_code == 404
    assert scenes.load(media, asset.id) is None


def test_cli_scenes_by_path_and_by_asset_id(workspace):
    out = runner.invoke(app, ["scenes", "streams/coding.mp4", "--root", str(workspace), "--json"])
    assert out.exit_code == 0, out.output
    doc = json.loads(out.stdout)
    assert doc["made"] is True and doc["asset"].startswith("a_") and _near([s["time"] for s in doc["scenes"]])
    assert len(doc["activity"]) == 26

    again = json.loads(runner.invoke(app, ["scenes", doc["asset"], "--root", str(workspace), "--json"]).stdout)
    assert again["made"] is False and again["scenes"] == doc["scenes"]
    cut = json.loads(
        runner.invoke(
            app, ["scenes", doc["asset"], "--root", str(workspace), "--start", "11", "--end", "13", "--json"]
        ).stdout
    )
    assert [s["time"] for s in cut["scenes"]] == [12.0] and cut["activity_start"] == 11.0
    plain = runner.invoke(app, ["scenes", doc["asset"], "--root", str(workspace)])
    assert plain.exit_code == 0 and "Already analysed" in plain.output and "3 scene change(s)" in plain.output
    assert "00:12" in plain.output or "0:12" in plain.output
    strict = json.loads(
        runner.invoke(app, ["scenes", doc["asset"], "--root", str(workspace), "--threshold", "0.9", "--json"]).stdout
    )
    assert strict["made"] is True and strict["scenes"] == [] and strict["threshold"] == 0.9


def test_cli_errors_and_audio_only(workspace):
    assert runner.invoke(app, ["scenes", "missing.mp4", "--root", str(workspace)]).exit_code != 0
    unknown = runner.invoke(app, ["scenes", "a_0000000000000000", "--root", str(workspace)])
    assert unknown.exit_code == 1 and "unknown asset" in unknown.output
    assert (
        runner.invoke(app, ["scenes", "streams/coding.mp4", "--root", str(workspace), "--threshold", "3"]).exit_code
        == 2
    )
    song = runner.invoke(app, ["scenes", "song.mp3", "--root", str(workspace), "--json"])
    assert song.exit_code == 0 and json.loads(song.stdout)["skipped"] == "audio-only asset"
    assert "Skipped" in runner.invoke(app, ["scenes", "song.mp3", "--root", str(workspace)]).output


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_analyze_scenes_and_get_scenes(workspace, anyio_backend):
    async with Client(create_server(workspace)) as client:
        await client.call_tool("scan_library", {})
        listed = (await client.call_tool("list_assets", {})).structured_content["assets"]
        video = next(a for a in listed if a["has_video"])
        song = next(a for a in listed if not a["has_video"])
        assert video["scenes"] is False
        assert (await client.call_tool("get_scenes", {"asset": video["id"]})).is_error
        done = (await client.call_tool("analyze_scenes", {"asset": video["id"]})).structured_content
        assert done["made"] is True and done["scenes"] == 3 and done["source"] == "original"
        assert (await client.call_tool("analyze_scenes", {"asset": video["id"]})).structured_content["made"] is False
        doc = (await client.call_tool("get_scenes", {"asset": video["id"]})).structured_content
        assert doc["asset"] == video["id"] and _near([s["time"] for s in doc["scenes"]])
        cut = (await client.call_tool("get_scenes", {"asset": video["id"], "start": 4, "end": 6})).structured_content
        assert [s["time"] for s in cut["scenes"]] == [5.0] and cut["activity_start"] == 4.0
        listed = (await client.call_tool("list_assets", {})).structured_content["assets"]
        assert next(a for a in listed if a["id"] == video["id"])["scenes"] is True
        skipped = (await client.call_tool("analyze_scenes", {"asset": song["id"]})).structured_content
        assert skipped["skipped"] == "audio-only asset"
        assert (await client.call_tool("analyze_scenes", {"asset": video["id"], "threshold": 5})).is_error
