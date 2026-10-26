"""Highlight review and make-short through the HTTP API, the CLI and MCP (SPL-64): each surface reviews a suggestion,
makes shorts on the recording's own timeline and the engine's rules show through (revisions, 9:16 crop, labels)."""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from split_video import assets, highlight_review
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.local_media_store import LocalMediaStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()


@pytest.fixture
def planted(tmp_path):
    """A workspace with a real 8 s 320x180 video (the routes probe it): its asset, no signals needed."""
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=10:duration=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=8",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            str(root / "mixed.mp4"),
        ],
        check=True,
        timeout=60,
    )
    return root, assets.register_path(LocalMediaStore(root), "mixed.mp4")


def timeline(client):
    got = client.get("/api/v1/project/timeline", params={"path": "mixed.mp4"})
    assert got.status_code == 200, got.text
    return got.json()


def test_http_review_and_make_short(planted):
    root, asset = planted
    client = TestClient(create_app(root, DEFAULTS))
    cand = {"start": 1.0, "end": 5.0, "title_hint": "The opener"}
    key = highlight_review.key_of(cand["start"], cand["end"])
    base = f"/api/v1/assets/{asset.id}/highlight-reviews"

    assert client.get(base).json() == {"version": 1, "revision": 0, "reviews": {}}
    window = {"start": cand["start"], "end": cand["end"]}
    got = client.put(f"{base}/{key}", json={"state": "dismissed", **window}, headers={"If-Match": '"0"'})
    assert got.status_code == 200 and got.json()["revision"] == 1
    stale = client.put(f"{base}/{key}", json={"state": None, **window}, headers={"If-Match": '"0"'})
    assert stale.status_code == 409 and stale.json()["error"] == "revision_conflict"
    assert client.put(f"{base}/{key}", json={"state": None, **window}).json()["reviews"] == {}
    assert client.put(f"{base}/h_1_2", json={"state": "accepted", **window}).status_code == 422
    assert client.put(f"{base}/{key}", json={"state": "maybe", **window}).status_code == 422
    assert client.get("/api/v1/assets/a_0000000000000000/highlight-reviews").status_code == 404

    revision = timeline(client)["revision"]
    body = {
        "asset": asset.id,
        "kind": "short",
        "items": [{"start": cand["start"], "end": cand["end"], "title": cand.get("title_hint", "")}],
    }
    stale = client.post(
        "/api/v1/project/highlights", params={"path": "mixed.mp4"}, json=body, headers={"If-Match": '"99"'}
    )
    assert stale.status_code == 409 and stale.json()["current_revision"] == revision
    made = client.post(
        "/api/v1/project/highlights", params={"path": "mixed.mp4"}, json=body, headers={"If-Match": f'"{revision}"'}
    )
    assert made.status_code == 200, made.text
    out = made.json()
    assert out["revision"] == revision + 1 and out["made"][0]["key"] == key
    assert out["reviews"]["reviews"][key]["state"] == "accepted"
    shown = timeline(client)
    assert shown["revision"] == revision + 1
    clips = [c for t in shown["tracks"] for c in t["clips"]]
    short = next(c for c in clips if c["id"] == out["made"][0]["clip"])
    assert short["label"].startswith("Short: ") and (short["in_point"], short["out_point"]) == (
        cand["start"],
        cand["end"],
    )
    assert short["crop"] is not None
    assert (
        client.post("/api/v1/project/highlights", params={"path": "mixed.mp4"}, json={**body, "items": []}).status_code
        == 422
    )
    assert client.post("/api/v1/project/highlights", params={"path": "nope.mp4"}, json=body).status_code == 404
    assert "/api/v1/project/highlights" in client.get("/openapi.json").json()["paths"]


def test_cli_review_and_make_short(planted):
    root, asset = planted
    cmd = ["--root", str(root), "--json"]
    out = runner.invoke(
        app, ["review-highlight", asset.id, "--start", "2", "--end", "0:00:06", "--state", "accepted", *cmd]
    )
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout)["reviews"]["h_2000_6000"]["state"] == "accepted"
    listed = json.loads(runner.invoke(app, ["highlight-reviews", "mixed.mp4", *cmd]).stdout)
    assert listed["revision"] == 1
    stale = runner.invoke(
        app,
        [
            "review-highlight",
            asset.id,
            "--start",
            "2",
            "--end",
            "6",
            "--state",
            "clear",
            "--base-revision",
            "0",
            *cmd,
        ],
    )
    assert stale.exit_code == 3
    made = runner.invoke(app, ["make-short", "mixed.mp4", "--range", "1-3", "--range", "0:00:04-0:00:07", *cmd])
    assert made.exit_code == 0, made.output
    doc = json.loads(made.stdout)
    assert [m["label"] for m in doc["made"]] == ["Short: Highlight 1", "Short: Highlight 2"]
    client = TestClient(create_app(root, DEFAULTS))
    assert timeline(client)["revision"] == doc["revision"]
    bad = runner.invoke(app, ["make-short", "mixed.mp4", "--range", "oops", *cmd])
    assert bad.exit_code != 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_review_and_make_short(planted, anyio_backend):
    root, asset = planted
    async with Client(create_server(root)) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert {"get_highlight_reviews", "review_highlight", "make_short"} <= names
        done = await mcp.call_tool("review_highlight", {"asset": asset.id, "start": 1, "end": 5, "state": "dismissed"})
        assert not done.is_error and done.structured_content["revision"] == 1
        bad = await mcp.call_tool("review_highlight", {"asset": asset.id, "start": 1, "end": 5, "state": "no"})
        assert bad.is_error
        got = await mcp.call_tool("get_highlight_reviews", {"asset": asset.id})
        assert list(got.structured_content["reviews"]) == ["h_1000_5000"]
        made = await mcp.call_tool(
            "make_short",
            {
                "video": "mixed.mp4",
                "asset": asset.id,
                "ranges": [{"start": 1, "end": 5, "title": "Failing test"}],
            },
        )
        assert not made.is_error, made.content
        assert made.structured_content["made"][0]["label"] == "Short: Failing test"
        assert made.structured_content["reviews"]["reviews"]["h_1000_5000"]["state"] == "accepted"
        stale = await mcp.call_tool(
            "make_short",
            {"video": "mixed.mp4", "asset": asset.id, "ranges": [{"start": 5, "end": 25}], "base_revision": 0},
        )
        assert stale.is_error
