"""Structured, actionable errors on every surface (SPL-55): the same `{code, message, fix, details?}` comes out of the
CLI (typer runner and a real subprocess), the MCP server (in memory and over real stdio) and the HTTP API for the
common mistakes: overlap, bad time, missing asset, missing timeline, stale revision, unknown preset, missing extra."""

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from test_plan_surfaces import (
    DEFAULTS,
    media_dir,  # noqa: F401  (a fixture)
    plan_for,
    workspace,
)
from typer.testing import CliRunner

from split_video import edit_plan, transcribe
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.mcp_server import create_server

runner = CliRunner()
MISSING_ASSET = "a_0000000000000000"


@pytest.fixture
def ws(media_dir, tmp_path):  # noqa: F811
    root, ids = workspace(media_dir, tmp_path, "ws")
    edit_plan.apply_plan(LocalMediaStore(root), LocalProjectStore(root), plan_for(ids))
    return root, ids


def payload_ok(body, code):
    """`body` is a structured error with `code`, a message and a fix naming a next action."""
    assert body["code"] == code, body
    assert body["message"] and len(body["fix"]) > 30, body
    return body


def cli_error(root, *args, code):
    result = runner.invoke(app, [*args, "--root", str(root), "--json"])
    assert result.exit_code != 0, result.stdout
    body = payload_ok(json.loads(result.stdout), code)
    assert "Error:" in result.stderr and "Fix:" in result.stderr
    return body, result


def mcp_error(result, code):
    assert result.is_error
    text = result.content[0].text
    return payload_ok(json.loads(text[text.index("{") :]), code)


def http_error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["detail"] == body["message"] or code in ("revision.conflict", "plan.invalid", "ops.invalid")
    return payload_ok(body, code)


# --- CLI ----------------------------------------------------------------------------------------------------------------


def test_cli_overlap_bad_time_and_missing_asset(ws):
    root, ids = ws
    overlap, result = cli_error(
        root,
        "compose",
        "add-clip",
        "reel",
        "--asset",
        ids["blue"],
        "--track",
        "2",
        "--start",
        "2",
        "--out",
        "1",
        code="clip.overlap",
    )
    assert "until 3s" in overlap["message"] and "show_timeline" in overlap["fix"] and result.exit_code == 1
    beyond, _ = cli_error(root, "compose", "trim", "reel", "1", "--out", "99", code="time.out_of_range")
    assert beyond["details"]["max_seconds"] == 4.0 and "0 s to 4 s" in beyond["fix"]
    cli_error(root, "compose", "trim", "reel", "1", "--in=-1", code="time.negative")
    cli_error(root, "frame", "red.mp4", "99", code="time.out_of_range")
    bad = cli_error(root, "frame", "red.mp4", "abc", code="time.invalid")[0]
    assert "1:23.5" in bad["fix"]
    asset = cli_error(root, "compose", "add-clip", "reel", "--asset", MISSING_ASSET, code="asset.not_found")[0]
    assert "list_assets" in asset["fix"] and "library list" in asset["fix"]
    cli_error(root, "compose", "add-clip", "reel", "nope.mp4", code="source.not_found")
    cli_error(root, "compose", "show", "ghost", code="timeline.not_found")


def test_cli_stale_revision_exits_3_and_unknown_preset_lists_choices(ws):
    root, ids = ws
    stale, result = cli_error(root, "compose", "add-track", "reel", "--base-revision", "0", code="revision.conflict")
    assert result.exit_code == 3 and stale["error"] == "revision_conflict" and stale["current_revision"] == 1
    assert stale["details"]["current_revision"] == 1 and "--base-revision 1" in stale["fix"]
    preset, _ = cli_error(root, "compose", "render", "reel", "--preset", "zzz", code="preset.unknown")
    assert "vertical-1080" in preset["fix"] and preset["details"]["choices"].startswith("draft-720")
    cli_error(root, "compose", "render", "reel", "--fit", "stretch", code="fit.unknown")
    style, _ = cli_error(root, "compose", "render", "reel", "--captions", "zzz", code="caption.style_unknown")
    assert "list_caption_styles" in style["fix"]


def test_cli_missing_extra_exits_4_with_the_install_command(ws, monkeypatch):
    root, ids = ws

    def missing(models_dir=None):
        raise transcribe.TranscriptionUnavailableError("ModuleNotFoundError: faster_whisper")

    monkeypatch.setattr(transcribe, "_import_faster_whisper", missing)
    body, result = cli_error(root, "transcribe", "red.mp4", code="extra.transcription_missing")
    assert result.exit_code == 4 and "uv sync --extra transcribe" in body["fix"]


def test_cli_without_json_prints_error_and_fix_to_stderr_only(ws):
    root, ids = ws
    result = runner.invoke(app, ["compose", "trim", "reel", "1", "--out", "99", "--root", str(root)])
    assert result.exit_code == 1 and result.stdout == ""
    assert "past the end of the source" in result.stderr and "Fix:" in result.stderr
    forced = runner.invoke(app, ["--json-errors", "compose", "trim", "reel", "1", "--out", "99", "--root", str(root)])
    assert json.loads(forced.stdout)["code"] == "time.out_of_range"


def test_cli_plan_and_ops_failures_keep_their_report_and_gain_code_and_fix(ws, tmp_path):
    root, ids = ws
    plan = plan_for(ids)
    plan["tracks"][0]["clips"][0]["out"] = 99
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    result = runner.invoke(app, ["apply-plan", str(path), "--root", str(root), "--dry-run"])
    body = json.loads(result.stdout)
    assert result.exit_code == 1 and body["error"] == "plan_invalid" and body["code"] == "plan.invalid"
    assert "plan.clip.out_of_range" in [e["code"] for e in body["errors"]] and body["fix"].count("(plan.") == 1
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({"schema_version": 1, "project": "reel", "ops": [{"op": "remove_clip", "clip": "nope"}]}))
    result = runner.invoke(app, ["apply-ops", str(ops), "--root", str(root)])
    body = json.loads(result.stdout)
    assert result.exit_code == 1 and body["code"] == "ops.invalid" and body["errors"][0]["code"] == "ops.clip.not_found"


def test_a_real_cli_subprocess_prints_the_json_and_exits_nonzero(ws):
    root, ids = ws
    run = subprocess.run(
        [sys.executable, "-c", "from split_video.cli import app; app()", "compose", "trim", "reel", "1", "--out", "99",
         "--root", str(root), "--json"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert run.returncode == 1
    body = json.loads(run.stdout)
    assert body["code"] == "time.out_of_range" and "list_assets" in body["fix"]
    assert "Error:" in run.stderr and "Fix:" in run.stderr


# --- MCP ----------------------------------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_in_memory_returns_structured_tool_errors(ws, anyio_backend, monkeypatch):
    root, ids = ws
    async with Client(create_server(root)) as mcp:

        async def call(tool, **args):
            return await mcp.call_tool(tool, args)

        overlap = mcp_error(
            await call("add_clip_to_timeline", name="reel", asset=ids["blue"], track=2, start=2.0, out_point=1.0),
            "clip.overlap",
        )
        assert "until 3s" in overlap["message"]
        beyond = mcp_error(await call("trim_timeline_clip", name="reel", clip=1, out_point=99.0), "time.out_of_range")
        assert beyond["details"]["max_seconds"] == 4.0
        mcp_error(await call("trim_timeline_clip", name="reel", clip=1, in_point=-1.0), "time.negative")
        mcp_error(await call("get_frame", asset=ids["red"], time=99.0), "time.out_of_range")
        mcp_error(await call("add_clip_to_timeline", name="reel", asset=MISSING_ASSET), "asset.not_found")
        mcp_error(await call("add_clip_to_timeline", name="reel", video="nope.mp4"), "source.not_found")
        mcp_error(await call("add_clip_to_timeline", name="reel", video="../x.mp4"), "source.outside_root")
        mcp_error(await call("show_timeline", name="ghost"), "timeline.not_found")
        stale = mcp_error(await call("add_timeline_track", name="reel", base_revision=0), "revision.conflict")
        assert stale["error"] == "revision_conflict" and stale["current_revision"] == 1
        preset = mcp_error(await call("render_timeline", name="reel", preset="zzz"), "preset.unknown")
        assert "youtube-1080p" in preset["fix"]
        mcp_error(await call("render_timeline", name="reel", fit="stretch"), "fit.unknown")
        mcp_error(await call("render_timeline", name="reel", captions="zzz"), "caption.style_unknown")
        # A tool's own argument check is `request.invalid`.
        mcp_error(await call("trim_timeline_clip", name="reel", clip=1), "request.invalid")
        bad_plan = plan_for(ids)
        bad_plan["tracks"][0]["clips"][0]["out"] = 99
        plan = mcp_error(await call("apply_plan", plan=bad_plan, dry_run=True), "plan.invalid")
        assert plan["error"] == "plan_invalid" and "plan.clip.out_of_range" in [e["code"] for e in plan["errors"]]
        ops = {"schema_version": 1, "project": "reel", "ops": [{"op": "remove_clip", "clip": "nope"}]}
        assert mcp_error(await call("apply_ops", ops=ops), "ops.invalid")["errors"][0]["code"] == "ops.clip.not_found"
        # Split projects: overlap-style and bad-time mistakes.
        mcp_error(await call("add_split", video="red.mp4", at=99.0), "time.out_of_range")
        mcp_error(await call("add_split", video="ghost.mp4", at=1.0), "source.not_found")
        mcp_error(await call("remove_split", video="red.mp4", split=3), "segments.not_found")

        def missing(models_dir=None):
            raise transcribe.TranscriptionUnavailableError("no faster_whisper")

        monkeypatch.setattr(transcribe, "_import_faster_whisper", missing)
        extra = mcp_error(await call("transcribe_asset", asset=ids["red"]), "extra.transcription_missing")
        assert "WITH_TRANSCRIPTION=1" in extra["fix"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_a_real_stdio_subprocess(ws, anyio_backend):
    root, ids = ws
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(root)]
    )
    async with Client(params) as mcp:
        overlap = mcp_error(
            await mcp.call_tool(
                "add_clip_to_timeline",
                {"name": "reel", "asset": ids["blue"], "track": 2, "start": 2.0, "out_point": 1.0},
            ),
            "clip.overlap",
        )
        beyond = mcp_error(
            await mcp.call_tool("trim_timeline_clip", {"name": "reel", "clip": 1, "out_point": 99.0}),
            "time.out_of_range",
        )
        missing = mcp_error(
            await mcp.call_tool("add_clip_to_timeline", {"name": "reel", "asset": MISSING_ASSET}), "asset.not_found"
        )
    assert overlap["fix"] and beyond["details"]["max_seconds"] == 4.0 and "list_assets" in missing["fix"]


# --- HTTP ---------------------------------------------------------------------------------------------------------------


def test_http_errors_keep_their_status_and_detail_and_add_code_and_fix(ws, monkeypatch):
    root, ids = ws
    client = TestClient(create_app(root, DEFAULTS))
    project = client.get("/api/v1/compose/projects/reel").json()
    overlay = project["tracks"][1]["id"]
    main_clip = project["tracks"][0]["clips"][0]["id"]

    overlap = client.post(
        "/api/v1/compose/projects/reel/clips",
        json={"asset": ids["blue"], "track_id": overlay, "start": 2.0, "out_point": 1.0},
    )
    assert "until 3s" in http_error(overlap, 422, "clip.overlap")["message"]
    beyond = client.patch(f"/api/v1/compose/projects/reel/clips/{main_clip}", json={"out_point": 99.0})
    assert http_error(beyond, 422, "time.out_of_range")["details"]["max_seconds"] == 4.0
    frame = client.get(f"/api/v1/assets/{ids['red']}/frame", params={"time": 99})
    http_error(frame, 422, "time.out_of_range")
    missing = client.post("/api/v1/compose/projects/reel/clips", json={"asset": MISSING_ASSET})
    assert "list_assets" in http_error(missing, 404, "asset.not_found")["fix"]
    http_error(client.post("/api/v1/compose/projects/reel/clips", json={"path": "nope.mp4"}), 404, "source.not_found")
    http_error(client.get("/api/v1/compose/projects/ghost"), 404, "timeline.not_found")
    exists = client.post("/api/v1/compose/projects", json={"name": "reel"})
    http_error(exists, 409, "timeline.exists")
    stale = client.post("/api/v1/compose/projects/reel/tracks", json={"kind": "audio"}, headers={"If-Match": "0"})
    body = http_error(stale, 409, "revision.conflict")
    assert body["error"] == "revision_conflict" and body["current_revision"] == 1 and "If-Match" in body["fix"]
    assert "Send the revision as the If-Match header" in body["detail"]
    preset = client.post("/api/v1/compose/projects/reel/render", json={"preset": "zzz"})
    assert "vertical-1080" in http_error(preset, 422, "preset.unknown")["fix"]
    batch = client.post(
        "/api/v1/compose/render-queue", json={"renders": [{"project": "ghost"}, {"project": "reel", "fit": "x"}]}
    )
    jobs = batch.json()["jobs"]
    assert [j["error"]["code"] for j in jobs] == ["timeline.not_found", "fit.unknown"]
    assert all(j["error"]["fix"] and j["error"]["message"] for j in jobs)
    http_error(client.post("/api/v1/compose/render-queue", json={"renders": []}), 422, "render.request_invalid")
    bad_plan = plan_for(ids)
    bad_plan["tracks"][0]["clips"][0]["out"] = 99
    plan = client.put("/api/v1/compose/projects/reel/plan", json=bad_plan, params={"dry_run": "true"})
    pbody = http_error(plan, 422, "plan.invalid")
    assert pbody["error"] == "plan_invalid" and "plan.clip.out_of_range" in [e["code"] for e in pbody["errors"]]
    ops = client.post(
        "/api/v1/compose/projects/reel/ops",
        json={"schema_version": 1, "ops": [{"op": "remove_clip", "clip": "nope"}]},
    )
    assert http_error(ops, 422, "ops.invalid")["errors"][0]["code"] == "ops.clip.not_found"

    def unavailable(models_dir=None):
        raise transcribe.TranscriptionUnavailableError("no faster_whisper")

    monkeypatch.setattr(transcribe, "_import_faster_whisper", unavailable)
    extra = client.post(f"/api/v1/assets/{ids['red']}/transcribe", json={})
    assert "uv sync --extra transcribe" in http_error(extra, 501, "extra.transcription_missing")["fix"]


def test_the_openapi_schema_documents_the_error_fields(ws):
    root, ids = ws
    schema = TestClient(create_app(root, DEFAULTS)).get("/openapi.json").json()["components"]["schemas"]
    for name in ("RevisionConflictOut", "PlanInvalidOut", "OpsInvalidOut"):
        assert {"code", "message", "fix"} <= set(schema[name]["properties"]), name
    assert {"code", "message", "fix", "detail"} <= set(schema["ApiErrorOut"]["properties"])
    assert "fix" in schema["RenderQueueErrorOut"]["properties"]
