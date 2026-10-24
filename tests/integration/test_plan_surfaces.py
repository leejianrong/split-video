"""Edit plans through the HTTP API, the CLI and MCP (in memory and over a real stdio subprocess): the same JSON must
come out of every surface, and a plan from real tiny media must render (SPL-49)."""

import json
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from split_video import assets, edit_plan, timeline_service
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.ffmpeg import probe_duration
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()


def _ffmpeg(path, *inputs, codec=("-c:v", "libx264", "-pix_fmt", "yuv420p")):
    cmd = ["ffmpeg", "-y", *[a for i in inputs for a in ("-f", "lavfi", "-i", i)], *codec, str(path)]
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory):
    """Four tiny real files, made once and copied into each workspace so they have the same asset ids everywhere."""
    src = tmp_path_factory.mktemp("plan_media")
    video = "color=c={}:size=160x120:rate=10:duration={}"
    _ffmpeg(
        src / "red.mp4",
        video.format("red", 4),
        "sine=frequency=440:duration=4",
        codec=("-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"),
    )
    _ffmpeg(src / "blue.mp4", video.format("blue", 3))
    _ffmpeg(
        src / "green.mp4",
        video.format("green", 6),
        "sine=frequency=330:duration=6",
        codec=("-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"),
    )
    _ffmpeg(src / "tone.m4a", "sine=frequency=220:duration=5", codec=("-c:a", "aac"))
    return src


def workspace(media_dir, tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    for f in media_dir.iterdir():
        shutil.copy(f, root / f.name)
    media = LocalMediaStore(root)
    ids = {f.stem: assets.register_path(media, f.name).id for f in sorted(media_dir.iterdir())}
    return root, ids


def plan_for(ids, **extra):
    return {
        "schema_version": 1,
        "project": "reel",
        "meta": {"by": "test"},
        "tracks": [
            {
                "kind": "video",
                "clips": [
                    {"asset": ids["red"], "in": 0, "out": 3},
                    {"asset": ids["blue"], "in": 0, "out": 2, "transition": "crossfade", "transition_duration": 0.5},
                    {"asset": ids["green"], "in": 1, "out": 4},
                ],
            },
            {
                "kind": "video",
                "magnetic": False,
                "clips": [{"asset": ids["blue"], "in": 0, "out": 2, "start": 1, "scale": 0.3, "x": 0.8, "y": 0.2}],
            },
            {"kind": "audio", "magnetic": False, "clips": [{"asset": ids["tone"], "in": 0, "out": 5, "gain": 0.5}]},
            *([] if "tracks_extra" not in extra else extra.pop("tracks_extra")),
        ],
        **extra,
    }


# --- a plan from real media renders ----------------------------------------------------------------------------------


def test_a_five_clip_plan_renders_to_the_expected_duration(media_dir, tmp_path):
    root, ids = workspace(media_dir, tmp_path, "ws")
    media, store = LocalMediaStore(root), LocalProjectStore(root)
    result = edit_plan.apply_plan(media, store, plan_for(ids))
    assert result["clips"] == 5 and result["duration"] == pytest.approx(3 + 2 - 0.5 + 3)
    project, filename, out_id = timeline_service.plan_render(store, media, "reel", "out.mp4", False)
    timeline_service.run_render(project, media, out_id)
    assert probe_duration(root / filename) == pytest.approx(7.5, abs=0.4)


# --- the same JSON on every surface ----------------------------------------------------------------------------------


def cli_apply(root, plan, *args, stdin=False, tmp_path=None):
    if stdin:
        return runner.invoke(app, ["apply-plan", "-", "--root", str(root), "--json", *args], input=json.dumps(plan))
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return runner.invoke(app, ["apply-plan", str(path), "--root", str(root), "--json", *args])


def tool_error(result):
    """The JSON object after "Error executing tool <name>: " in a failed tool call."""
    assert result.is_error
    text = result.content[0].text
    assert text.startswith("Error executing tool")
    return json.loads(text[text.index("{") :])


async def mcp_call(client, tool, /, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


def test_http_cli_and_engine_agree_on_apply_dry_run_and_schema(media_dir, tmp_path):
    results = {}
    for surface in ("engine", "http", "http-path", "cli", "cli-stdin"):
        root, ids = workspace(media_dir, tmp_path, surface)
        plan = plan_for(ids)
        if surface == "engine":
            results[surface] = edit_plan.apply_plan(LocalMediaStore(root), LocalProjectStore(root), plan)
        elif surface == "http":
            client = TestClient(create_app(root, DEFAULTS))
            got = client.put("/api/v1/compose/projects/reel/plan", json=plan)
            assert got.status_code == 200, got.text
            results[surface] = got.json()
            assert client.get("/api/v1/compose/projects/reel").json()["revision"] == 1
        elif surface == "http-path":  # the URL names the project; the body may leave it out
            client = TestClient(create_app(root, DEFAULTS))
            body = {k: v for k, v in plan.items() if k != "project"}
            got = client.put("/api/v1/compose/projects/reel/plan", json=body)
            assert got.status_code == 200, got.text
            results[surface] = got.json()
        else:
            out = cli_apply(root, plan, stdin=surface == "cli-stdin", tmp_path=tmp_path)
            assert out.exit_code == 0, out.output
            results[surface] = json.loads(out.stdout)
    first = results["engine"]
    assert first["clips"] == 5 and first["revision"] == 1 and first["meta"] == {"by": "test"}
    assert all(r == first for r in results.values()), results

    # Dry run: same document with dry_run true, and nothing written anywhere.
    root, ids = workspace(media_dir, tmp_path, "dry")
    client = TestClient(create_app(root, DEFAULTS))
    http = client.put("/api/v1/compose/projects/reel/plan", params={"dry_run": "true"}, json=plan_for(ids))
    cli = cli_apply(root, plan_for(ids), "--dry-run", tmp_path=tmp_path)
    assert http.status_code == 200 and cli.exit_code == 0
    dry = http.json()
    assert dry == json.loads(cli.stdout)
    assert dry.pop("timeline")["name"] == "reel"  # a dry run also shows the whole project (SPL-50, additive)
    assert dry == {**first, "dry_run": True}
    assert client.get("/api/v1/compose/projects").json()["projects"] == []

    # The schema: one document everywhere.
    shown = runner.invoke(app, ["plan-schema"])
    assert shown.exit_code == 0 and shown.stdout == edit_plan.schema_json()
    got = client.get("/api/v1/schemas/edit-plan")
    assert got.status_code == 200 and got.json() == edit_plan.schema() == json.loads(shown.stdout)
    paths = client.get("/openapi.json").json()
    assert "/api/v1/compose/projects/{name}/plan" in paths["paths"] and "/api/v1/schemas/edit-plan" in paths["paths"]
    assert "schemas/edit-plan" in paths["info"]["description"]


def test_invalid_plans_report_everything_on_every_surface(media_dir, tmp_path):
    root, ids = workspace(media_dir, tmp_path, "bad")
    plan = plan_for(ids)
    plan["tracks"][0]["clips"][0]["out"] = 99
    plan["tracks"][0]["clips"][1]["asset"] = "a_ffffffffffffffff"
    plan["tracks"][1]["clips"][0]["scale"] = 50
    plan["colour"] = "red"
    expected = [e.to_dict() for e in edit_plan.validate_plan(LocalMediaStore(root), plan).errors]
    assert len(expected) == 4

    client = TestClient(create_app(root, DEFAULTS))
    http = client.put("/api/v1/compose/projects/reel/plan", json=plan)
    assert http.status_code == 422
    body = http.json()
    assert body["error"] == "plan_invalid" and body["ok"] is False and body["errors"] == expected
    assert "4 problems" in body["detail"]
    cli = cli_apply(root, plan, tmp_path=tmp_path)
    assert cli.exit_code == 1
    assert json.loads(cli.stdout) == body  # the same document, with the catalogue's code, message and fix (SPL-55)
    assert body["code"] == "plan.invalid" and body["fix"]
    assert LocalProjectStore(root).list_timelines() == []  # nothing was created by any of them

    mismatch = client.put("/api/v1/compose/projects/other/plan", json=plan_for(ids))
    assert mismatch.status_code == 422 and mismatch.json()["errors"][0]["code"] == "plan.project.mismatch"
    assert (
        client.put("/api/v1/compose/projects/reel/plan", json=[1]).json()["errors"][0]["code"]
        == "plan.document.not_object"
    )
    unreadable = tmp_path / "bad.json"
    unreadable.write_text("{not json")
    assert runner.invoke(app, ["apply-plan", str(unreadable), "--root", str(root)]).exit_code != 0
    assert runner.invoke(app, ["apply-plan", str(tmp_path / "nope.json"), "--root", str(root)]).exit_code != 0


def test_revisions_on_every_surface(media_dir, tmp_path):
    root, ids = workspace(media_dir, tmp_path, "rev")
    client = TestClient(create_app(root, DEFAULTS))
    assert client.put("/api/v1/compose/projects/reel/plan", json=plan_for(ids)).status_code == 200
    again = plan_for(ids, mode="replace")
    # Without a revision: the plan itself is invalid, a 422 with the code that says what to send.
    got = client.put("/api/v1/compose/projects/reel/plan", json=again)
    assert got.status_code == 422 and got.json()["errors"][0]["code"] == "plan.replace.needs_base_revision"
    stale = client.put("/api/v1/compose/projects/reel/plan", json=again, headers={"If-Match": '"0"'})
    assert (
        stale.status_code == 409
        and stale.json()["error"] == "revision_conflict"
        and stale.json()["current_revision"] == 1
    )
    cli = cli_apply(root, again, "--base-revision", "0", tmp_path=tmp_path)
    assert cli.exit_code == 3 and json.loads(cli.stdout)["error"] == "revision_conflict"
    ok = client.put("/api/v1/compose/projects/reel/plan", json=again, headers={"If-Match": '"1"'})
    assert ok.status_code == 200 and ok.json()["revision"] == 2 and ok.json()["action"] == "replaced"
    ok = cli_apply(root, again, "--base-revision", "2", tmp_path=tmp_path)
    assert ok.exit_code == 0 and json.loads(ok.stdout)["revision"] == 3
    loud = client.put(
        "/api/v1/compose/projects/reel/plan", json=plan_for(ids, mode="replace", replace_unconditionally=True)
    )
    assert loud.status_code == 200 and loud.json()["revision"] == 4
    exists = client.put("/api/v1/compose/projects/reel/plan", json=plan_for(ids))
    assert exists.status_code == 422 and exists.json()["errors"][0]["code"] == "plan.project.exists"


def test_cli_text_output(media_dir, tmp_path):
    root, ids = workspace(media_dir, tmp_path, "text")
    (tmp_path / "p.json").write_text(json.dumps(plan_for(ids)))
    out = runner.invoke(app, ["apply-plan", str(tmp_path / "p.json"), "--root", str(root), "--dry-run"])
    assert out.exit_code == 0 and "would be created" in out.output and "5 clips" in out.output
    out = runner.invoke(app, ["apply-plan", str(tmp_path / "p.json"), "--root", str(root)])
    assert out.exit_code == 0 and "'reel' created" in out.output
    shown = runner.invoke(app, ["compose", "show", "reel", "--root", str(root), "--json"])
    assert len([c for t in json.loads(shown.stdout)["tracks"] for c in t["clips"]]) == 5


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_returns_the_same_json_in_memory(media_dir, tmp_path, anyio_backend):
    engine_root, ids = workspace(media_dir, tmp_path, "engine")
    expected = edit_plan.apply_plan(LocalMediaStore(engine_root), LocalProjectStore(engine_root), plan_for(ids))
    root, _ = workspace(media_dir, tmp_path, "mcp")
    async with Client(create_server(root)) as mcp:
        assert {"apply_plan", "get_edit_plan_schema"} <= {t.name for t in (await mcp.list_tools()).tools}
        assert await mcp_call(mcp, "get_edit_plan_schema") == edit_plan.schema()
        dry = await mcp_call(mcp, "apply_plan", plan=plan_for(ids), dry_run=True)
        assert dry.pop("timeline")["revision"] == dry["revision"]
        assert dry == {**expected, "dry_run": True}
        assert await mcp_call(mcp, "apply_plan", plan=plan_for(ids)) == expected
        # Invalid: a tool error whose text is the compact report with every problem.
        bad = plan_for(ids, mode="create")
        bad["project"] = "other"
        bad["tracks"][0]["clips"][0]["out"] = 99
        bad["tracks"][1]["clips"][0]["scale"] = 50
        failed = await mcp.call_tool("apply_plan", {"plan": bad})
        assert failed.is_error
        report = tool_error(failed)
        assert report["error"] == "plan_invalid" and [e["code"] for e in report["errors"]] == [
            "plan.clip.out_of_range",
            "plan.field.range",
        ]
        assert [e["path"] for e in report["errors"]] == ["/tracks/0/clips/0/out", "/tracks/1/clips/0/scale"]
        # Stale revision: the usual revision_conflict error.
        stale = await mcp.call_tool("apply_plan", {"plan": plan_for(ids, mode="replace"), "base_revision": 0})
        assert stale.is_error and tool_error(stale)["error"] == "revision_conflict"
        fresh = await mcp_call(mcp, "apply_plan", plan=plan_for(ids, mode="replace"), base_revision=1)
        assert (fresh["revision"], fresh["action"]) == (2, "replaced")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_a_real_stdio_subprocess(media_dir, tmp_path, anyio_backend):
    engine_root, ids = workspace(media_dir, tmp_path, "engine")
    expected = edit_plan.apply_plan(LocalMediaStore(engine_root), LocalProjectStore(engine_root), plan_for(ids))
    root, _ = workspace(media_dir, tmp_path, "stdio")
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(root)]
    )
    async with Client(params) as mcp:
        assert await mcp_call(mcp, "apply_plan", plan=plan_for(ids)) == expected
        assert await mcp_call(mcp, "get_edit_plan_schema") == edit_plan.schema()
        failed = await mcp.call_tool("apply_plan", {"plan": plan_for(ids, mode="replace")})
        assert "plan.replace.needs_base_revision" in tool_error(failed)["errors"][0]["code"]
    # What the subprocess saved is what the engine saved.
    assert LocalProjectStore(root).load_existing_timeline("reel") == LocalProjectStore(
        engine_root
    ).load_existing_timeline("reel")
