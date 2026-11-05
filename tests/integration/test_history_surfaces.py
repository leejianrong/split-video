"""Undo and redo (SPL-75) on the HTTP API, the CLI and MCP: the same scenario, the same results and the same errors."""

import json

import anyio
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
HISTORY = {"can_undo": True, "can_redo": True, "undo_label": "Set canvas", "redo_label": "Set canvas"}


def seen(shown):
    return shown["revision"], shown["canvas"]["ratio"], shown["history"]


def cli(root, *args, code=0):
    out = runner.invoke(app, ["compose", *args, "--root", str(root), "--json"])
    assert out.exit_code == code, out.output
    return json.loads(out.stdout)


def ops(ratio):
    return {"schema_version": 1, "ops": [{"op": "set_canvas", "ratio": ratio}]}


def test_undo_and_redo_are_the_same_on_http_cli_and_mcp(tmp_path):
    results = {}

    root = tmp_path / "http"
    root.mkdir()
    client = TestClient(create_app(root, DEFAULTS))
    base = "/api/v1/compose/projects/reel"
    client.post("/api/v1/compose/projects", json={"name": "reel"})
    for ratio in ("1:1", "9:16"):
        assert client.post(f"{base}/ops", json=ops(ratio)).status_code == 200
    assert client.get(base).json()["history"]["undo_label"] == "Set canvas"
    stale = client.post(f"{base}/undo", headers={"If-Match": '"1"'})
    assert stale.status_code == 409 and stale.json()["error"] == "revision_conflict"
    undone = client.post(f"{base}/undo", headers={"If-Match": '"2"'})
    assert undone.status_code == 200, undone.text
    redone = client.post(f"{base}/redo")
    results["http"] = (seen(undone.json()), seen(redone.json()))
    empty = client.post(f"{base}/redo")
    assert empty.status_code == 422 and empty.json()["code"] == "history.empty"

    root = tmp_path / "cli"
    root.mkdir()
    cli(root, "new", "reel")
    for ratio in ("1:1", "9:16"):
        cli(root, "set-canvas", "reel", ratio)
    assert cli(root, "show", "reel")["history"]["undo_label"] == "Set canvas"
    assert cli(root, "undo", "reel", "--base-revision", "1", code=3)["error"] == "revision_conflict"
    undone = cli(root, "undo", "reel", "--base-revision", "2")
    redone = cli(root, "redo", "reel")
    results["cli"] = (seen(undone), seen(redone))
    assert cli(root, "redo", "reel", code=1)["code"] == "history.empty"

    root = tmp_path / "mcp"
    root.mkdir()

    async def run_mcp():
        async with Client(create_server(root)) as mcp:
            await mcp_call(mcp, "create_timeline", name="reel")
            for ratio in ("1:1", "9:16"):
                await mcp_call(mcp, "apply_ops", name="reel", ops={**ops(ratio), "project": "reel"})
            assert (await mcp_call(mcp, "show_timeline", name="reel"))["history"]["undo_label"] == "Set canvas"
            stale = await mcp.call_tool("undo", {"name": "reel", "base_revision": 1})
            assert tool_error(stale)["error"] == "revision_conflict"
            undone = await mcp_call(mcp, "undo", name="reel", base_revision=2)
            redone = await mcp_call(mcp, "redo", name="reel")
            results["mcp"] = (seen(undone), seen(redone))
            assert tool_error(await mcp.call_tool("redo", {"name": "reel"}))["code"] == "history.empty"

    anyio.run(run_mcp)

    assert results["http"] == results["cli"] == results["mcp"]
    undone, redone = results["http"]
    assert undone == (3, "1:1", HISTORY)
    assert redone == (4, "9:16", {**HISTORY, "can_redo": False, "redo_label": None})


def test_a_recordings_timeline_is_undone_over_http(media_dir, tmp_path):  # noqa: F811
    root, _ = workspace(media_dir, tmp_path, "video")
    client = TestClient(create_app(root, DEFAULTS))
    params = {"path": "red.mp4"}
    first = client.get("/api/v1/project/timeline", params=params).json()
    assert first["history"]["can_undo"] is False
    clip_id = first["tracks"][0]["clips"][0]["id"]
    split = {"schema_version": 1, "ops": [{"op": "split_clip", "clip": clip_id, "at": 1.5}]}
    assert client.post("/api/v1/project/ops", params=params, json=split).status_code == 200

    shown = client.get("/api/v1/project/timeline", params=params).json()
    assert len(shown["tracks"][0]["clips"]) == 2 and shown["history"]["undo_label"] == "Split clip"
    undone = client.post("/api/v1/project/undo", params=params, headers={"If-Match": f'"{shown["revision"]}"'})
    assert undone.status_code == 200, undone.text
    assert len(undone.json()["tracks"][0]["clips"]) == 1 and undone.json()["revision"] == shown["revision"] + 1
    assert undone.json()["history"]["can_redo"] is True
    redone = client.post("/api/v1/project/redo", params=params)
    assert len(redone.json()["tracks"][0]["clips"]) == 2
    assert client.post("/api/v1/project/redo", params=params).json()["code"] == "history.empty"
