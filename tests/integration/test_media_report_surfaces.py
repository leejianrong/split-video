"""The media report through the HTTP API, the CLI and MCP (in memory and over a real stdio subprocess): the same
JSON must come out of every surface (SPL-44). Signals are planted; `prepare` runs on a real short clip."""

import json
import shutil
import subprocess
import sys
import time

import pytest
from fake_signals import make_store, plant
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from split_video import assets, media_report
from split_video.cli import app
from split_video.editor import classify
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()


@pytest.fixture
def planted(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    media = make_store("local", root, path="talk.mp4")
    return root, plant(media)


def _cli(root, *args):
    out = runner.invoke(app, ["report", *args, "--root", str(root), "--json"])
    assert out.exit_code == 0, out.output
    return out.stdout


async def _call(client, /, **arguments):
    result = await client.call_tool("media_report", arguments)
    assert not result.is_error, result.content
    return result.structured_content


def _wait(client, job_id, timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_http_cli_and_engine_agree(planted):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/report"
    expected = media_report.media_report(LocalMediaStore(root), asset.id)
    got = client.get(base)
    assert got.status_code == 200 and got.json() == expected
    assert got.content == media_report.dumps(expected).encode()  # byte for byte what the CLI prints
    assert _cli(root, asset.id).strip() == got.text and _cli(root, "talk.mp4").strip() == got.text
    for bucket, include in ((30, None), (50, "speech,audio")):
        params = {"bucket_seconds": bucket} | ({"include": include} if include else {})
        flags = ["--bucket-seconds", str(bucket)] + (["--include", include] if include else [])
        one = client.get(base, params=params)
        assert one.json()["buckets"]["seconds"] == bucket
        assert json.loads(_cli(root, asset.id, *flags)) == one.json()


def test_http_errors(planted):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/report"
    assert client.get("/api/v1/assets/a_0000000000000000/report").status_code == 404
    assert client.post("/api/v1/assets/a_0000000000000000/report/prepare").status_code == 404
    for params in ({"bucket_seconds": 1}, {"bucket_seconds": 9999}, {"include": "nope"}, {"bucket_seconds": "x"}):
        found = client.get(base, params=params)
        assert found.status_code == 422 and found.json()["detail"], params
    openapi = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/assets/{asset_id}/report" in openapi and "/api/v1/assets/{asset_id}/report/prepare" in openapi


def test_http_get_never_computes(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    media = make_store("local", root, path="talk.mp4")
    asset = plant(media, skip=("loudness", "silence", "audio"))
    client = TestClient(create_app(root, DEFAULTS))
    body = client.get(f"/api/v1/assets/{asset.id}/report").json()
    assert [m["signal"] for m in body["signals"]["missing"]] == ["audio", "loudness", "silence"]
    assert media.load_cache(asset.path, "waveform") is None  # nothing was computed or stored


def test_cli_text_summary_and_errors(planted):
    root, asset = planted
    shown = runner.invoke(app, ["report", asset.id, "--root", str(root)])
    assert shown.exit_code == 0, shown.output
    for word in ("talk.mp4", "signals:", "loudness:", "speech", "scene changes: 2", "transcript: 10 words", "welcome"):
        assert word in shown.output, word
    for args in (
        ["nope.mp4"],
        ["a_0000000000000000"],
        [asset.id, "--bucket-seconds", "1"],
        [asset.id, "--include", "nope"],
    ):
        assert runner.invoke(app, ["report", *args, "--root", str(root)]).exit_code != 0, args


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_tool_returns_the_same_json(planted, anyio_backend):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/report"
    async with Client(create_server(root)) as mcp:
        assert "media_report" in {t.name for t in (await mcp.list_tools()).tools}
        assert await _call(mcp, asset=asset.id) == client.get(base).json()
        assert (
            await _call(mcp, asset=asset.id, bucket_seconds=30, include=["speech"])
            == client.get(base, params={"bucket_seconds": 30, "include": "speech"}).json()
        )
        for args in ({"bucket_seconds": 1}, {"include": ["nope"]}):
            assert (await mcp.call_tool("media_report", {"asset": asset.id, **args})).is_error
        assert (await mcp.call_tool("media_report", {"asset": "a_0000000000000000"})).is_error


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_a_real_stdio_subprocess(planted, anyio_backend):
    root, asset = planted
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(root)]
    )
    async with Client(params) as mcp:
        found = await _call(mcp, asset=asset.id, bucket_seconds=30)
    client = TestClient(create_app(root, DEFAULTS))
    assert client.get(f"/api/v1/assets/{asset.id}/report", params={"bucket_seconds": 30}).json() == found
    assert json.loads(_cli(root, asset.id, "--bucket-seconds", "30")) == found


# --- prepare ----------------------------------------------------------------------------------------------


@pytest.fixture
def clip_root(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    root = tmp_path / "ws"
    root.mkdir()
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
    cmd += ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=3", "-filter_complex", "[0][1]concat=n=2:v=0:a=1"]
    subprocess.run([*cmd, str(root / "clip.wav")], check=True, capture_output=True)
    return root


def test_http_prepare_job_then_report(clip_root, monkeypatch):
    if not classify.MODEL_PATH.exists():
        monkeypatch.setattr(media_report.project, "run_analysis", lambda *a, **k: None)
    media = LocalMediaStore(clip_root)
    asset = assets.register_path(media, "clip.wav")
    client = TestClient(create_app(clip_root, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}/report"
    assert client.get(base).json()["signals"]["available"]["loudness"] is False
    job = _wait(client, client.post(f"{base}/prepare").json()["job_id"])
    assert job["kind"] == "analysis" and job["state"] == "done" and job["completed"] == job["total"] == 1000
    assert job["result"]["asset"] == asset.id and job["result"]["prepared"][:2] == ["loudness", "silence"]
    body = client.get(base).json()
    assert body["signals"]["available"]["loudness"] and body["signals"]["available"]["silence"]
    assert [m["signal"] for m in body["signals"]["missing"] if m["signal"] != "audio"] == ["speech"]


def test_http_cancelled_prepare_job_stores_no_classification(clip_root, monkeypatch):
    def hang(media, source, cache, thresholds, on_progress=None, cancel=None):
        while True:  # a model run that never ends on its own
            cancel.raise_if_cancelled()
            time.sleep(0.02)

    monkeypatch.setattr(media_report.project, "run_analysis", hang)
    media = LocalMediaStore(clip_root)
    asset = assets.register_path(media, "clip.wav")
    client = TestClient(create_app(clip_root, DEFAULTS))
    job_id = client.post(f"/api/v1/assets/{asset.id}/report/prepare").json()["job_id"]
    deadline = time.time() + 30
    while client.get(f"/api/v1/jobs/{job_id}").json()["completed"] < 150 and time.time() < deadline:
        time.sleep(0.02)  # the loudness and silence stages are done, the model run is "running"
    client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert _wait(client, job_id)["state"] == "cancelled"
    assert media.load_cache(asset.path, "classification") is None
    body = client.get(f"/api/v1/assets/{asset.id}/report").json()
    assert body["signals"]["available"]["loudness"] and body["signals"]["available"]["audio"] is False


@pytest.mark.skipif(not classify.MODEL_PATH.exists(), reason="needs the YAMNet model (make fetch-model)")
def test_cli_prepare_and_mcp_prepare_for_real(clip_root, tmp_path):
    out = runner.invoke(app, ["report", "clip.wav", "--root", str(clip_root), "--json", "--prepare"])
    assert out.exit_code == 0, out.output
    doc = json.loads(out.stdout)
    assert doc["prepared"]["prepared"] == ["loudness", "silence", "classification"]
    assert doc["signals"]["available"]["audio"] and doc["signals"]["available"]["loudness"]
    text = runner.invoke(app, ["report", "clip.wav", "--root", str(clip_root)])
    assert text.exit_code == 0 and "speech" in text.output  # the missing transcript hint
    client = TestClient(create_app(clip_root, DEFAULTS))
    assert client.get(f"/api/v1/assets/{doc['asset']['id']}/report").json() == {**doc, "prepared": None}
