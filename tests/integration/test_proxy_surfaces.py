"""Proxy and thumbnails through the HTTP API (as jobs), the CLI and MCP, with real ffmpeg on a tiny clip."""

import json
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from split_video import assets, proxy
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


def _gig(root):
    media = LocalMediaStore(root.resolve())
    return media, assets.register_path(media, "shows/gig.mp4")


def test_api_proxy_job_then_serve_with_ranges_and_thumbnails(workspace):
    media, asset = _gig(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    base = f"/api/v1/assets/{asset.id}"
    assert client.get(f"{base}/proxy").status_code == 404
    assert client.get(f"{base}/thumbnails").status_code == 404
    assert client.get(f"{base}/thumbnails/info").status_code == 404
    listed = client.get("/api/v1/assets").json()["assets"]
    assert [(a["proxy"], a["thumbnails"]) for a in listed] == [(False, False)]

    job = _wait(client, client.post(f"{base}/proxy").json()["job_id"])
    assert job["kind"] == "proxy" and job["state"] == "done"
    assert job["completed"] == job["total"] == proxy.STEPS and job["result"]["made"] == ["proxy", "thumbnails"]

    full = client.get(f"{base}/proxy")
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
    assert full.headers["accept-ranges"] == "bytes"
    size = len(full.content)
    part = client.get(f"{base}/proxy", headers={"Range": f"bytes={size // 2}-{size // 2 + 99}"})
    assert part.status_code == 206 and part.content == full.content[size // 2 : size // 2 + 100]
    assert part.headers["content-range"] == f"bytes {size // 2}-{size // 2 + 99}/{size}"
    tail = client.get(f"{base}/proxy", headers={"Range": "bytes=-50"})
    assert tail.status_code == 206 and tail.content == full.content[-50:]

    info = client.get(f"{base}/thumbnails/info").json()
    sprite = client.get(f"{base}/thumbnails")
    assert sprite.headers["content-type"] == "image/jpeg" and sprite.content[:2] == b"\xff\xd8"
    sidecar = workspace / "shows" / ".split-video-cache" / "gig.mp4.thumbs.jpg"
    dims = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(sidecar)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert dims.strip() == f"{info['width']},{info['height']}"
    assert (info["tile_width"] * info["columns"], info["tile_height"] * info["rows"]) == (info["width"], info["height"])
    assert info["interval"] == 10.0 and info["count"] == 3 and info["source"] == "proxy"

    listed = client.get("/api/v1/assets").json()["assets"]
    assert (listed[0]["proxy"], listed[0]["thumbnails"]) == (True, True)
    assert client.get(f"{base}").json()["proxy"] is True

    # A second request is a no-op job.
    again = _wait(client, client.post(f"{base}/proxy").json()["job_id"])
    assert again["result"]["made"] == []


def test_api_errors(workspace):
    media, asset = _gig(workspace)
    audio = assets.register_path(media, "song.mp3")
    client = TestClient(create_app(workspace, DEFAULTS))
    assert client.post(f"/api/v1/assets/{audio.id}/proxy").status_code == 422
    assert client.post("/api/v1/assets/a_0000000000000000/proxy").status_code == 404
    assert client.get("/api/v1/assets/a_0000000000000000/proxy").status_code == 404
    assert client.get(f"/api/v1/assets/{audio.id}/thumbnails").status_code == 404


def test_api_scan_with_proxies_queues_a_job_per_new_video(workspace):
    client = TestClient(create_app(workspace, DEFAULTS))
    plain = _wait(client, client.post("/api/v1/library/scan", json={}).json()["job_id"])
    assert "proxy_jobs" not in plain["result"]  # off by default

    shutil.rmtree(workspace / ".split-video-cache", ignore_errors=True)
    (workspace / ".split-video-assets.json").unlink()
    job = _wait(client, client.post("/api/v1/library/scan", json={"proxies": True}).json()["job_id"])
    (queued,) = job["result"]["proxy_jobs"]  # the mp3 gets none
    assert _wait(client, queued["job_id"])["state"] == "done"
    (video,) = [a for a in client.get("/api/v1/assets").json()["assets"] if a["has_video"]]
    assert video["id"] == queued["asset"] and video["proxy"] and video["thumbnails"]


def test_api_cancel_leaves_no_partial_proxy(workspace, monkeypatch):
    media, asset = _gig(workspace)
    client = TestClient(create_app(workspace, DEFAULTS))
    started = []
    real = proxy._run

    def slow(cmd, on_seconds, cancel, what):
        started.append(1)
        cmd[cmd.index("-i") + 1 : cmd.index("-i") + 1] = []
        return real([cmd[0], "-re", *cmd[1:]], on_seconds, cancel, what)  # -re: real time, so there is time to cancel

    monkeypatch.setattr(proxy, "_run", slow)
    job_id = client.post(f"/api/v1/assets/{asset.id}/proxy").json()["job_id"]
    deadline = time.time() + 20
    while not started and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.5)
    client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert _wait(client, job_id)["state"] == "cancelled"
    sidecar = workspace / "shows" / ".split-video-cache"
    assert not [p for p in sidecar.glob("*") if "proxy" in p.name or "thumbs" in p.name]
    assert client.get(f"/api/v1/assets/{asset.id}/proxy").status_code == 404


def test_cli_scan_proxies_then_proxies_is_a_no_op_then_list(workspace):
    out = runner.invoke(app, ["library", "scan", str(workspace), "--proxies", "--json"])
    assert out.exit_code == 0, out.output
    report = json.loads(out.stdout)
    (made,) = report["proxies"]
    assert made["made"] == ["proxy", "thumbnails"]

    again = json.loads(runner.invoke(app, ["library", "proxies", str(workspace), "--json"]).stdout)
    assert again == {"assets": []}  # nothing pending
    forced = json.loads(runner.invoke(app, ["library", "proxies", str(workspace), "--force", "--json"]).stdout)
    assert [r["made"] for r in forced["assets"]] == [["proxy", "thumbnails"]]
    one = json.loads(
        runner.invoke(app, ["library", "proxies", str(workspace), "--asset", made["asset"], "--json"]).stdout
    )
    assert one["assets"][0]["made"] == []

    rows = json.loads(runner.invoke(app, ["library", "list", str(workspace), "--json"]).stdout)["assets"]
    assert {r["path"]: (r["proxy"], r["thumbnails"]) for r in rows} == {
        "shows/gig.mp4": (True, True),
        "song.mp3": (False, False),
    }
    bad = runner.invoke(app, ["library", "proxies", str(workspace), "--asset", "a_0000000000000000"])
    assert bad.exit_code == 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_prepare_asset(workspace, anyio_backend):
    async with Client(create_server(workspace)) as client:
        await client.call_tool("scan_library", {})
        listed = (await client.call_tool("list_assets", {})).structured_content["assets"]
        video = next(a for a in listed if a["has_video"])
        assert video["proxy"] is False
        done = (await client.call_tool("prepare_asset", {"asset": video["id"]})).structured_content
        assert done["made"] == ["proxy", "thumbnails"] and done["thumbnails"]["columns"] == 3
        again = (await client.call_tool("prepare_asset", {"asset": video["id"]})).structured_content
        assert again["made"] == []
        audio = next(a for a in listed if not a["has_video"])
        skipped = (await client.call_tool("prepare_asset", {"asset": audio["id"]})).structured_content
        assert skipped["skipped"]
        assert (await client.call_tool("prepare_asset", {"asset": "a_0000000000000000"})).is_error
        listed = (await client.call_tool("list_assets", {})).structured_content["assets"]
        assert next(a for a in listed if a["has_video"])["proxy"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_scan_with_proxies(workspace, anyio_backend):
    async with Client(create_server(workspace)) as client:
        scan = (await client.call_tool("scan_library", {"proxies": True})).structured_content
        assert [p["made"] for p in scan["proxies"]] == [["proxy", "thumbnails"]]
