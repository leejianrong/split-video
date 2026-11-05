"""`POST /project/clips` and the `split` job on the HTTP API, the CLI and MCP: one scenario per capability, the same
result and the same stale-revision error everywhere (SPL-120). Upload is browser-only (docs/api.md)."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from test_plan_surfaces import (
    DEFAULTS,
    mcp_call,
    media_dir,  # noqa: F401  (a fixture)
    tool_error,
    workspace,
)
from typer.testing import CliRunner

from split_video.cli import app
from split_video.editor.app import create_app
from split_video.mcp_server import create_server

runner = CliRunner()
VIDEO = "red.mp4"
SPLIT_ARGS = {"min_silence_duration": 0.5, "min_song_length": 1.0, "label_prefix": "Song"}


def clips_of(tracks):
    return [(c["asset"], c["source"], c["start"], c["end"], c["duration"]) for c in tracks[0]["clips"]]


def wait_job(client, job_id):
    deadline = time.time() + 60
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def cli_json(*args):
    out = runner.invoke(app, [*args, "--json"])
    return out.exit_code, json.loads(out.stdout)


@pytest.mark.anyio
async def test_add_clip_is_the_same_on_http_cli_and_mcp(media_dir, tmp_path):  # noqa: F811
    results = {}
    http_root, ids = workspace(media_dir, tmp_path, "http")
    client = TestClient(create_app(http_root, DEFAULTS))
    params = {"path": VIDEO}
    body = {"asset": ids["green"], "index": 0}
    stale = client.post("/api/v1/project/clips", params=params, json=body, headers={"If-Match": '"7"'})
    assert stale.status_code == 409 and stale.json()["error"] == "revision_conflict"
    got = client.post("/api/v1/project/clips", params=params, json=body, headers={"If-Match": '"0"'})
    assert got.status_code == 200, got.text
    results["http"] = (got.json()["revision"], got.json()["duration"], clips_of(got.json()["tracks"]))

    cli_root, ids = workspace(media_dir, tmp_path, "cli")
    base = ["add-asset-clip", str(cli_root / VIDEO), "--asset", ids["green"], "--at", "1"]
    code, err = cli_json(*base, "--base-revision", "7")
    assert code == 3 and err["error"] == "revision_conflict"
    code, shown = cli_json(*base, "--base-revision", "0")
    assert code == 0, shown
    results["cli"] = (shown["revision"], shown["duration"], clips_of(shown["tracks"]))

    mcp_root, ids = workspace(media_dir, tmp_path, "mcp")
    async with Client(create_server(mcp_root)) as mcp:
        args = {"video": VIDEO, "asset": ids["green"], "position": 1}
        stale = await mcp.call_tool("add_asset_to_video", {**args, "base_revision": 7})
        assert tool_error(stale)["error"] == "revision_conflict"
        shown = await mcp_call(mcp, "add_asset_to_video", **args, base_revision=0)
        results["mcp"] = (shown["revision"], shown["duration"], clips_of(shown["tracks"]))
        audio_only = await mcp.call_tool("add_asset_to_video", {"video": VIDEO, "asset": ids["tone"]})
        assert tool_error(audio_only)["code"] == "media.audio_only"

    assert results["http"] == results["cli"] == results["mcp"]
    revision, _, clips = results["http"]
    assert revision == 1 and clips[0][0] == ids["green"] and len(clips) == 2


@pytest.mark.anyio
async def test_split_result_is_the_same_on_http_cli_and_mcp(media_dir, tmp_path):  # noqa: F811
    results = {}
    http_root, ids = workspace(media_dir, tmp_path, "http")
    client = TestClient(create_app(http_root, DEFAULTS))
    url = f"/api/v1/assets/{ids['red']}/split"
    stale = wait_job(client, client.post(url, json=SPLIT_ARGS, headers={"If-Match": '"7"'}).json()["job_id"])
    assert stale["state"] == "error" and stale["error_info"]["code"] == "revision.conflict"
    job = wait_job(client, client.post(url, json=SPLIT_ARGS, headers={"If-Match": '"0"'}).json()["job_id"])
    assert job["state"] == "done", job
    results["http"] = job["result"]

    cli_root, _ = workspace(media_dir, tmp_path, "cli")
    base = [
        "split-by-silence",
        str(cli_root / VIDEO),
        "--min-silence-duration",
        "0.5",
        "--min-song-length",
        "1",
        "--label-prefix",
        "Song",
    ]
    code, err = cli_json(*base, "--base-revision", "7")
    assert code == 3 and err["error"] == "revision_conflict"
    code, got = cli_json(*base, "--base-revision", "0")
    assert code == 0, got
    results["cli"] = got

    mcp_root, _ = workspace(media_dir, tmp_path, "mcp")
    async with Client(create_server(mcp_root)) as mcp:
        stale = await mcp.call_tool("split_by_silence", {"video": VIDEO, **SPLIT_ARGS, "base_revision": 7})
        assert tool_error(stale)["error"] == "revision_conflict"
        results["mcp"] = await mcp_call(mcp, "split_by_silence", video=VIDEO, **SPLIT_ARGS, base_revision=0)
        mute = await mcp.call_tool("split_by_silence", {"video": "blue.mp4"})
        assert mute.is_error

    assert results["http"] == results["cli"] == results["mcp"]
    assert set(results["http"]) == {"video", "segments", "revision", "labels"} and results["http"]["revision"] == 1
