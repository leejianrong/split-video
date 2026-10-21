"""Catalogue gaps and output polish on every surface (SPL-112): MCP argument errors, HTTP 404/422 bodies, scan rows,
the render-queue CLI's failure lines, rounded outputs and no stray lock files."""

import json
import sys

import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from test_errors_surfaces import mcp_error
from test_plan_surfaces import (
    DEFAULTS,
    media_dir,  # noqa: F401  (a fixture)
    plan_for,
    workspace,
)
from typer.testing import CliRunner

from split_video import edit_plan
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.mcp_server import create_server

runner = CliRunner()


@pytest.fixture
def ws(media_dir, tmp_path):  # noqa: F811
    root, ids = workspace(media_dir, tmp_path, "ws")
    edit_plan.apply_plan(LocalMediaStore(root), LocalProjectStore(root), plan_for(ids))
    return root, ids


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_argument_validation_is_a_structured_request_invalid(ws, anyio_backend):
    root, ids = ws
    async with Client(create_server(root)) as mcp:
        bad = mcp_error(await mcp.call_tool("apply_plan", {"plan": "str"}), "request.invalid")
        assert "pydantic" not in json.dumps(bad) and "validation error for" not in json.dumps(bad)
        assert bad["details"]["errors"][0]["argument"] == "plan"
        assert "plan" in bad["message"] and "First problem, `plan`" in bad["fix"]
        missing = mcp_error(await mcp.call_tool("get_frame", {"asset": ids["red"]}), "request.invalid")
        assert missing["details"]["errors"][0] == {"argument": "time", "problem": "Field required", "type": "missing"}
        wrong = mcp_error(await mcp.call_tool("get_frame", {"asset": ids["red"], "time": "abc"}), "request.invalid")
        assert "time" in wrong["message"] and "valid number" in wrong["message"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_argument_validation_over_real_stdio(ws, anyio_backend):
    root, _ = ws
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(root)]
    )
    async with Client(params) as mcp:
        bad = mcp_error(await mcp.call_tool("apply_plan", {"plan": "str"}), "request.invalid")
    assert bad["fix"] and bad["details"]["errors"][0]["argument"] == "plan"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_highlight_and_export_presets_each_get_their_own_fix(ws, anyio_backend):
    root, ids = ws
    async with Client(create_server(root)) as mcp:
        highlight = mcp_error(
            await mcp.call_tool("suggest_highlights", {"asset": ids["red"], "preset": "zzz"}), "preset.unknown"
        )
        assert highlight["fix"].startswith("Use one of: default, gig, stream, lecture")
        export = mcp_error(await mcp.call_tool("render_timeline", {"name": "reel", "preset": "zzz"}), "preset.unknown")
        assert "`list_export_presets`" in export["fix"] and "suggest_highlights" not in export["fix"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_outputs_are_rounded_to_a_millisecond(ws, anyio_backend):
    root, ids = ws
    async with Client(create_server(root)) as mcp:
        clips = [{"asset": ids["green"], "in": 0.1 * i + 0.00049, "out": 0.1 * i + 0.00049 + 0.3} for i in range(1, 7)]
        plan = {"schema_version": 1, "project": "noisy", "tracks": [{"kind": "video", "clips": clips}]}
        applied = (await mcp.call_tool("apply_plan", {"plan": plan})).structured_content
        shown = (await mcp.call_tool("show_timeline", {"name": "noisy"})).structured_content
        crop = (
            await mcp.call_tool(
                "set_clip_crop",
                {"name": "noisy", "clip": 1, "x": 0.1 + 0.2, "y": 0.0, "w": 0.316406 + 1e-12, "h": 0.5625},
            )
        ).structured_content
    assert applied["duration"] == round(applied["duration"], 3)
    assert all(t["end"] == round(t["end"], 3) for t in applied["tracks"])
    for doc in (shown, crop):
        assert doc["duration"] == round(doc["duration"], 3)
        for clip in doc["tracks"][0]["clips"]:
            for key in ("in", "out", "start", "end", "duration"):
                assert clip[key] == round(clip[key], 3), (key, clip[key])
    assert crop["tracks"][0]["clips"][0]["crop"] == {"x": 0.3, "y": 0.0, "w": 0.316406, "h": 0.5625}
    stored = json.loads((root / "noisy.split-video-timeline.json").read_text())["tracks"][0]["clips"][0]
    assert stored["in"] == clips[0]["in"], "the stored value keeps full precision"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_scan_library_skipped_rows_have_code_fix_and_no_absolute_paths(ws, anyio_backend):
    root, _ = ws
    (root / "broken.mp4").write_bytes(b"not really a video" * 50)
    async with Client(create_server(root)) as mcp:
        scan = (await mcp.call_tool("scan_library", {})).structured_content
    (row,) = scan["skipped"]
    assert row["path"] == "broken.mp4" and row["code"] == "media.unreadable" and row["fix"]
    assert str(root) not in json.dumps(row) and "\n" not in row["reason"]
    assert (root / ".split-video-assets.json").exists()


def test_http_unknown_job_and_bad_query_have_code_and_fix(ws):
    root, ids = ws
    client = TestClient(create_app(root, DEFAULTS))
    job = client.get("/api/v1/jobs/nope")
    assert job.status_code == 404
    body = job.json()
    assert body["detail"] == "unknown job id"  # the legacy key, unchanged
    assert body["code"] == "job.unknown" and "render_queue_status" in body["fix"]
    assert client.post("/api/v1/jobs/nope/cancel").json()["code"] == "job.unknown"

    frame = client.get(f"/api/v1/assets/{ids['red']}/frame", params={"time": "abc"})
    assert frame.status_code == 422
    body = frame.json()
    assert isinstance(body["detail"], list) and body["detail"][0]["loc"] == ["query", "time"]  # FastAPI's own list
    assert body["code"] == "request.invalid" and "time" in body["message"] and body["fix"]
    assert body["details"]["errors"][0]["argument"] == "time" and body["details"]["errors"][0]["in"] == "query"
    # A body that fails its schema gets the same treatment.
    post = client.post("/api/v1/compose/projects/reel/clips", json={"track_id": 5})
    assert post.status_code == 422 and post.json()["code"] == "request.invalid"


def test_cli_render_queue_shows_code_and_fix_for_a_failed_render(ws, tmp_path):
    root, _ = ws
    spec = tmp_path / "queue.json"
    spec.write_text(json.dumps([{"project": "ghost", "output": "x"}, {"project": "reel", "output": "ok_out"}]))
    result = runner.invoke(app, ["compose", "render-queue", str(spec), "--root", str(root)])
    assert result.exit_code == 1
    lines = result.stderr.splitlines()
    failed = next(i for i, ln in enumerate(lines) if ln.startswith("[1/2]") and ": failed" in ln)
    assert "timeline.not_found" in lines[failed + 1]
    assert "Fix:" in lines[failed + 1] and "list_timelines" in lines[failed + 1]
    # JSON output is unchanged: the error is still `{code, message, fix}`.
    as_json = runner.invoke(app, ["compose", "render-queue", str(spec), "--root", str(root), "--json"])
    error = json.loads(as_json.stdout)["jobs"][0]["error"]
    assert error["code"] == "timeline.not_found" and error["fix"]


def test_writing_projects_leaves_no_lock_files_next_to_the_videos(ws):
    root, _ = ws
    names = sorted(p.name for p in root.iterdir())
    assert not [n for n in names if n.endswith(".lock")], names
    assert ".split-video-locks" in names
