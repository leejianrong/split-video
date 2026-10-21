"""Transcription through the HTTP API (as a job), the CLI and MCP, with a fake model (see tests/fake_whisper.py)
and a real ffmpeg decode. Real-model checks are in test_transcribe_real.py."""

import json
import subprocess
import sys
import time

import pytest
from fake_whisper import FakeModel, speech, write_wav
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from split_video import assets, transcribe
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()
STARTS = [0.5 + 1.1 * k for k in range(8)]


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "talks").mkdir(parents=True)
    pcm, _ = speech(STARTS, 10.0)
    write_wav(root / "talks" / "talk.wav", pcm)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:size=64x64:duration=2", "-an", str(root / "mute.mp4")],
        capture_output=True,
        check=True,
    )
    return root


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    """The surfaces run the engine's default path (`load_model`); give it the fake, as if the extra were installed."""
    monkeypatch.setattr(transcribe, "require_available", lambda: None)
    monkeypatch.setattr(transcribe, "load_model", lambda name, directory: FakeModel())


@pytest.fixture
def no_faster_whisper(monkeypatch, fake_model):
    monkeypatch.undo()  # the fake out, the real availability check back in...
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # ...with the package blocked


def _wait(client, job_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _talk(root):
    media = LocalMediaStore(root.resolve())
    return media, assets.register_path(media, "talks/talk.wav")


def test_api_transcribe_job_then_get_transcript(workspace):
    media, asset = _talk(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    assert client.get(f"{base}/transcript").status_code == 404
    assert client.get(base).json()["transcript"] is False

    job = _wait(client, client.post(f"{base}/transcribe").json()["job_id"])
    assert job["kind"] == "transcription" and job["state"] == "done"
    assert job["completed"] == job["total"] == 1000
    assert job["result"]["made"] is True and job["result"]["words"] == 8 and job["result"]["model"] == "base.en"

    doc = client.get(f"{base}/transcript").json()
    assert doc["asset"] == asset.id and doc["version"] == 1 and doc["language"] == "en"
    assert [len(s["words"]) for s in doc["segments"]] == [3, 3, 2]
    assert doc["segments"][0]["words"][0] == {"start": 0.5, "end": 0.9, "word": "w300", "probability": 0.9}
    cut = client.get(f"{base}/transcript", params={"start": 2.0, "end": 5.0}).json()
    assert [w["word"] for s in cut["segments"] for w in s["words"]] == ["w700", "w900", "w1100"]
    assert client.get(base).json()["transcript"] is True
    assert [a["transcript"] for a in client.get("/api/v1/assets").json()["assets"]] == [True]

    again = _wait(client, client.post(f"{base}/transcribe").json()["job_id"])
    assert again["result"]["made"] is False
    redo = _wait(client, client.post(f"{base}/transcribe", json={"force": True, "model": "small.en"}).json()["job_id"])
    assert redo["result"]["made"] is True and redo["result"]["model"] == "small.en"
    assert (workspace / ".split-video-models").is_dir()  # where a real model would go


def test_api_errors(workspace):
    media, _ = _talk(workspace)
    mute = assets.register_path(media, "mute.mp4")
    client = TestClient(create_app(workspace, DEFAULTS))
    assert client.post(f"/api/v1/assets/{mute.id}/transcribe").status_code == 422
    assert client.post("/api/v1/assets/a_0000000000000000/transcribe").status_code == 404
    assert client.get("/api/v1/assets/a_0000000000000000/transcript").status_code == 404


def test_api_without_the_dependency_is_501_with_the_hint_and_queues_nothing(workspace, no_faster_whisper):
    _, asset = _talk(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    got = client.post(f"/api/v1/assets/{asset.id}/transcribe")
    assert got.status_code == 501 and got.json()["detail"].startswith(transcribe.UNAVAILABLE)
    assert client.get("/api/v1/jobs").json()["jobs"] == []


def test_api_cancel_stops_a_running_transcription(workspace, monkeypatch):
    _, asset = _talk(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    started = []

    def slow(i):
        started.append(i)
        time.sleep(0.3)

    monkeypatch.setattr(transcribe, "load_model", lambda name, directory: FakeModel(per_segment=1, on_segment=slow))
    job_id = client.post(f"/api/v1/assets/{asset.id}/transcribe").json()["job_id"]
    deadline = time.time() + 20
    while not started and time.time() < deadline:
        time.sleep(0.02)
    client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert _wait(client, job_id)["state"] == "cancelled"
    assert client.get(f"/api/v1/assets/{asset.id}/transcript").status_code == 404
    assert len(started) < 8  # it did not run to the end


def test_cli_transcribe_by_path_and_by_asset_id_then_transcript(workspace):
    out = runner.invoke(app, ["transcribe", "talks/talk.wav", "--root", str(workspace), "--json"])
    assert out.exit_code == 0, out.output
    result = json.loads(out.stdout)
    assert result["made"] is True and result["words"] == 8 and result["asset"].startswith("a_")

    again = runner.invoke(app, ["transcribe", result["asset"], "--root", str(workspace), "--json"])
    assert json.loads(again.stdout)["made"] is False
    plain = runner.invoke(app, ["transcribe", result["asset"], "--root", str(workspace)])
    assert plain.exit_code == 0 and "Already transcribed" in plain.output and "8 words" in plain.output

    doc = json.loads(runner.invoke(app, ["transcript", "talks/talk.wav", "--root", str(workspace), "--json"]).stdout)
    assert doc["asset"] == result["asset"] and len([w for s in doc["segments"] for w in s["words"]]) == 8
    cut = json.loads(
        runner.invoke(
            app, ["transcript", result["asset"], "--root", str(workspace), "--start", "2", "--end", "5.0", "--json"]
        ).stdout
    )
    assert [w["word"] for s in cut["segments"] for w in s["words"]] == ["w700", "w900", "w1100"]
    text = runner.invoke(app, ["transcript", result["asset"], "--root", str(workspace)])
    assert text.exit_code == 0 and "w300 w500 w700" in text.output


def test_cli_errors(workspace):
    nope = runner.invoke(app, ["transcribe", "missing.wav", "--root", str(workspace)])
    assert nope.exit_code != 0
    unknown = runner.invoke(app, ["transcribe", "a_0000000000000000", "--root", str(workspace)])
    assert unknown.exit_code == 1 and "unknown asset" in unknown.output
    first = runner.invoke(app, ["transcript", "talks/talk.wav", "--root", str(workspace)])
    assert first.exit_code == 1 and "no transcript" in first.output


def test_cli_without_the_dependency_exits_4_with_the_hint(workspace, no_faster_whisper):
    out = runner.invoke(app, ["transcribe", "talks/talk.wav", "--root", str(workspace)])
    assert out.exit_code == 4
    flat = " ".join(out.output.split())  # rich wraps the message at the terminal width
    assert "uv sync --extra transcribe" in flat and "WITH_TRANSCRIPTION=1" in flat
    assert "pip install 'split-video[transcribe]'" in flat
    assert not (workspace / ".split-video-models").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_transcribe_asset_and_get_transcript(workspace, anyio_backend):
    async with Client(create_server(workspace)) as client:
        await client.call_tool("scan_library", {})
        (talk,) = [
            a
            for a in (await client.call_tool("list_assets", {})).structured_content["assets"]
            if a["has_audio"] and not a["has_video"]
        ]
        assert talk["transcript"] is False
        assert (await client.call_tool("get_transcript", {"asset": talk["id"]})).is_error
        done = (await client.call_tool("transcribe_asset", {"asset": talk["id"]})).structured_content
        assert done["made"] is True and done["words"] == 8 and done["model"] == "base.en"
        assert (await client.call_tool("transcribe_asset", {"asset": talk["id"]})).structured_content["made"] is False
        doc = (await client.call_tool("get_transcript", {"asset": talk["id"]})).structured_content
        assert doc["asset"] == talk["id"] and doc["segments"][0]["words"][0]["word"] == "w300"
        cut = (
            await client.call_tool("get_transcript", {"asset": talk["id"], "start": 2.0, "end": 5.0})
        ).structured_content
        assert [w["word"] for s in cut["segments"] for w in s["words"]] == ["w700", "w900", "w1100"]
        listed = (await client.call_tool("list_assets", {})).structured_content["assets"]
        assert next(a for a in listed if a["id"] == talk["id"])["transcript"] is True
        mute = next(a for a in listed if a["has_video"])
        assert (await client.call_tool("transcribe_asset", {"asset": mute["id"]})).is_error


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_without_the_dependency_is_a_tool_error_with_the_hint(workspace, no_faster_whisper, anyio_backend):
    async with Client(create_server(workspace)) as client:
        await client.call_tool("scan_library", {})
        listed = (await client.call_tool("list_assets", {})).structured_content["assets"]
        talk = next(a for a in listed if not a["has_video"])
        got = await client.call_tool("transcribe_asset", {"asset": talk["id"]})
        assert got.is_error and "uv sync --extra transcribe" in got.content[0].text


def _extra_gone(monkeypatch):
    """After a transcript is stored: the extra stops being importable, and any model use would fail loudly."""
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    def unavailable():
        raise transcribe.TranscriptionUnavailableError("no faster_whisper")

    monkeypatch.setattr(transcribe, "require_available", unavailable)


def test_api_reuses_a_stored_transcript_without_the_extra(workspace, monkeypatch):
    _, asset = _talk(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    url = f"/api/v1/assets/{asset.id}/transcribe"
    assert _wait(client, client.post(url).json()["job_id"])["result"]["made"] is True
    _extra_gone(monkeypatch)
    again = client.post(url)
    assert again.status_code == 200
    assert _wait(client, again.json()["job_id"])["result"]["made"] is False
    assert client.post(url, json={"force": True}).status_code == 501
    assert client.post(url, json={"model": "small.en"}).status_code == 501


def test_cli_reuses_a_stored_transcript_without_the_extra(workspace, monkeypatch):
    args = ["transcribe", "talks/talk.wav", "--root", str(workspace), "--json"]
    assert json.loads(runner.invoke(app, args).stdout)["made"] is True
    _extra_gone(monkeypatch)
    again = runner.invoke(app, args)
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["made"] is False
    assert runner.invoke(app, [*args, "--force"]).exit_code == 4


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_reuses_a_stored_transcript_without_the_extra(workspace, monkeypatch, anyio_backend):
    async with Client(create_server(workspace)) as client:
        await client.call_tool("scan_library", {})
        listed = (await client.call_tool("list_assets", {})).structured_content["assets"]
        talk = next(a for a in listed if not a["has_video"])
        assert (await client.call_tool("transcribe_asset", {"asset": talk["id"]})).structured_content["made"] is True
        _extra_gone(monkeypatch)
        again = await client.call_tool("transcribe_asset", {"asset": talk["id"]})
        assert not again.is_error and again.structured_content["made"] is False
        assert (await client.call_tool("transcribe_asset", {"asset": talk["id"], "force": True})).is_error
