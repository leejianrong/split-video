"""Edit-operation batches through the HTTP API, the CLI and MCP (in memory and over a real stdio subprocess): the same
JSON must come out of every surface, and edits made from real tiny media must render (SPL-50)."""

import json
import sys

import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from test_plan_surfaces import (
    DEFAULTS,
    mcp_call,
    media_dir,  # noqa: F401  (a fixture)
    plan_for,
    tool_error,
    workspace,
)
from typer.testing import CliRunner

from split_video import edit_ops, edit_plan, timeline_service
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.ffmpeg import probe_duration
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.mcp_server import create_server

runner = CliRunner()
NAME = "reel"


def seeded(media_dir, tmp_path, name):  # noqa: F811
    """A workspace holding project `reel` made from a plan (so its ids are the same in every workspace)."""
    root, ids = workspace(media_dir, tmp_path, name)
    edit_plan.apply_plan(LocalMediaStore(root), LocalProjectStore(root), plan_for(ids))
    return root, ids


def ops_for(ids, root=None):
    """Edits of the seeded project: a trim, a fade, a title on a new track, a moved overlay."""
    project = LocalProjectStore(root).load_timeline(NAME) if root else None
    main, overlay = project.tracks[0], project.tracks[1]
    return {
        "schema_version": 1,
        "project": NAME,
        "meta": {"by": "test"},
        "ops": [
            {"op": "trim_clip", "clip": main.clips[2].id, "in": 2, "out": 3.5},
            {"op": "set_transition", "clip": main.clips[1].id, "transition": "crossfade", "duration": 0.8},
            {"op": "add_track", "id": "titles", "kind": "video"},
            {"op": "add_text_clip", "id": "cap", "track": "titles", "text": "Hello", "duration": 2, "start": 1},
            {"op": "place_clip", "clip": overlay.clips[0].id, "start": 2.5},
            {"op": "add_clip", "id": "extra", "asset": ids["green"], "in": 0, "out": 1},
        ],
    }


def cli_ops(root, doc, *args, stdin=False, tmp_path=None):
    if stdin:
        return runner.invoke(app, ["apply-ops", "-", "--root", str(root), "--json", *args], input=json.dumps(doc))
    path = tmp_path / "ops.json"
    path.write_text(json.dumps(doc))
    return runner.invoke(app, ["apply-ops", str(path), "--root", str(root), "--json", *args])


def snapshot(root):
    return {
        p.name: p.read_bytes()
        for p in sorted(root.iterdir())
        if p.is_file() and (p.suffix == ".json" or p.name.startswith("."))
    }


def test_edits_from_real_media_render(media_dir, tmp_path):  # noqa: F811
    root, ids = seeded(media_dir, tmp_path, "ws")
    media, store = LocalMediaStore(root), LocalProjectStore(root)
    result = edit_ops.apply_ops(media, store, ops_for(ids, root))
    assert result["applied"] == 6 and result["revision"] == 2
    project, filename, out_id = timeline_service.plan_render(store, media, NAME, "out.mp4", False)
    timeline_service.run_render(project, media, out_id)
    assert probe_duration(root / filename) == pytest.approx(project.duration, abs=0.4)
    assert project.duration == pytest.approx(1 + 3 + 2 - 0.5 + 1.5 - 0.8 + 0.5 + 0.3, abs=2.5)


def test_http_cli_and_engine_agree_on_apply_dry_run_and_schema(media_dir, tmp_path):  # noqa: F811
    results = {}
    for surface in ("engine", "http", "http-path", "cli", "cli-stdin"):
        root, ids = seeded(media_dir, tmp_path, surface)
        doc = ops_for(ids, root)
        if surface == "engine":
            results[surface] = edit_ops.apply_ops(LocalMediaStore(root), LocalProjectStore(root), doc)
        elif surface in ("http", "http-path"):
            client = TestClient(create_app(root, DEFAULTS))
            body = {k: v for k, v in doc.items() if k != "project"} if surface == "http-path" else doc
            got = client.post(f"/api/v1/compose/projects/{NAME}/ops", json=body)
            assert got.status_code == 200, got.text
            results[surface] = got.json()
            assert client.get(f"/api/v1/compose/projects/{NAME}").json()["revision"] == 2
        else:
            out = cli_ops(root, doc, stdin=surface == "cli-stdin", tmp_path=tmp_path)
            assert out.exit_code == 0, out.output
            results[surface] = json.loads(out.stdout)
    first = results["engine"]
    assert first["applied"] == 6 and first["meta"] == {"by": "test"} and "timeline" not in first
    assert all(r == first for r in results.values()), results

    # Dry run: the same document plus the project, and not a byte written anywhere.
    root, ids = seeded(media_dir, tmp_path, "dry")
    doc = ops_for(ids, root)
    before = snapshot(root)
    client = TestClient(create_app(root, DEFAULTS))
    http = client.post(f"/api/v1/compose/projects/{NAME}/ops", params={"dry_run": "true"}, json=doc)
    cli = cli_ops(root, doc, "--dry-run", tmp_path=tmp_path)
    assert http.status_code == 200 and cli.exit_code == 0
    dry = http.json()
    assert dry == json.loads(cli.stdout) and dry["dry_run"] is True and dry["revision"] == 2
    view = dry.pop("timeline")
    assert view["name"] == NAME and view["revision"] == 2 and "titles" in [t["id"] for t in view["tracks"]]
    assert dry == {**first, "dry_run": True}
    assert snapshot(root) == before
    assert client.get(f"/api/v1/compose/projects/{NAME}").json()["revision"] == 1

    # The schema: one document everywhere.
    shown = runner.invoke(app, ["ops-schema"])
    assert shown.exit_code == 0 and shown.stdout == edit_ops.schema_json()
    got = client.get("/api/v1/schemas/edit-ops")
    assert got.status_code == 200 and got.json() == edit_ops.schema() == json.loads(shown.stdout)
    paths = client.get("/openapi.json").json()
    assert "/api/v1/compose/projects/{name}/ops" in paths["paths"] and "/api/v1/schemas/edit-ops" in paths["paths"]
    assert "schemas/edit-ops" in paths["info"]["description"]


def test_invalid_batches_report_the_same_thing_on_every_surface(media_dir, tmp_path):  # noqa: F811
    root, ids = seeded(media_dir, tmp_path, "bad")
    doc = ops_for(ids, root)
    doc["ops"][1] = {"op": "set_transition", "clip": "ghost", "transition": "cut"}
    before = snapshot(root)
    expected = edit_ops.invalid_body(edit_ops.validate_ops(LocalMediaStore(root), LocalProjectStore(root), doc))
    assert [e["path"] for e in expected["errors"]] == ["/ops/1/clip"] and expected["applied"] == 1

    client = TestClient(create_app(root, DEFAULTS))
    http = client.post(f"/api/v1/compose/projects/{NAME}/ops", json=doc)
    assert http.status_code == 422
    body = http.json()
    assert body["errors"] == expected["errors"] and body["error"] == "ops_invalid" and body["ok"] is False
    assert body["applied"] == 1 and [r["ok"] for r in body["results"]] == [True, False]
    cli = cli_ops(root, doc, tmp_path=tmp_path)
    assert cli.exit_code == 1
    cli_body = json.loads(cli.stdout)
    assert cli_body == body
    assert snapshot(root) == before  # nothing was written by any of them

    # dry run + continue_on_error: HTTP (body field), CLI (flag) give the same report
    many = {
        **doc,
        "ops": [
            *doc["ops"],
            {"op": "remove_clip", "clip": "ghost2"},
            {"op": "move_track", "track": "ghost3", "to_index": 0},
        ],
    }
    http = client.post(
        f"/api/v1/compose/projects/{NAME}/ops", params={"dry_run": "true"}, json={**many, "continue_on_error": True}
    )
    cli = cli_ops(root, {**many, "continue_on_error": True}, "--dry-run", "--continue-on-error", tmp_path=tmp_path)
    assert http.status_code == 422 and cli.exit_code == 1
    assert http.json() == json.loads(cli.stdout)
    assert [e["path"] for e in http.json()["errors"]] == ["/ops/1/clip", "/ops/6/clip", "/ops/7/track"]
    assert http.json()["timeline"]["name"] == NAME
    assert snapshot(root) == before

    mismatch = client.post("/api/v1/compose/projects/other/ops", json=doc)
    assert mismatch.status_code == 422 and mismatch.json()["errors"][0]["code"] == "ops.project.mismatch"
    assert (
        client.post(f"/api/v1/compose/projects/{NAME}/ops", json=[1]).json()["errors"][0]["code"]
        == "ops.document.not_object"
    )
    assert client.post("/api/v1/compose/projects/nothere/ops", json={**doc, "project": "nothere"}).status_code == 404
    bad_flag = client.post(f"/api/v1/compose/projects/{NAME}/ops", json={**doc, "continue_on_error": True})
    assert bad_flag.json()["errors"][0]["code"] == "ops.continue_on_error.needs_dry_run"
    unreadable = tmp_path / "bad.json"
    unreadable.write_text("{not json")
    assert runner.invoke(app, ["apply-ops", str(unreadable), "--root", str(root)]).exit_code != 0
    assert runner.invoke(app, ["apply-ops", str(tmp_path / "nope.json"), "--root", str(root)]).exit_code != 0


def test_revisions_on_every_surface(media_dir, tmp_path):  # noqa: F811
    root, ids = seeded(media_dir, tmp_path, "rev")
    doc = ops_for(ids, root)
    client = TestClient(create_app(root, DEFAULTS))
    before = snapshot(root)
    stale = client.post(f"/api/v1/compose/projects/{NAME}/ops", json=doc, headers={"If-Match": '"0"'})
    assert (
        stale.status_code == 409
        and stale.json()["error"] == "revision_conflict"
        and stale.json()["current_revision"] == 1
    )
    stale_dry = client.post(
        f"/api/v1/compose/projects/{NAME}/ops", params={"dry_run": "true"}, json=doc, headers={"If-Match": '"0"'}
    )
    assert stale_dry.status_code == 409
    cli = cli_ops(root, doc, "--base-revision", "0", tmp_path=tmp_path)
    assert cli.exit_code == 3 and json.loads(cli.stdout)["error"] == "revision_conflict"
    in_doc = client.post(f"/api/v1/compose/projects/{NAME}/ops", json={**doc, "base_revision": 0})
    assert in_doc.status_code == 409
    assert snapshot(root) == before
    ok = client.post(f"/api/v1/compose/projects/{NAME}/ops", json=doc, headers={"If-Match": '"1"'})
    assert ok.status_code == 200 and ok.json()["revision"] == 2
    again = {"schema_version": 1, "project": NAME, "ops": [{"op": "add_track", "kind": "audio"}]}
    ok = cli_ops(root, again, "--base-revision", "2", tmp_path=tmp_path)
    assert ok.exit_code == 0 and json.loads(ok.stdout)["revision"] == 3
    disagree = client.post(
        f"/api/v1/compose/projects/{NAME}/ops", json={**again, "base_revision": 3}, headers={"If-Match": '"2"'}
    )
    assert disagree.status_code == 422 and disagree.json()["errors"][0]["code"] == "ops.base_revision.conflict"


def test_cli_text_output(media_dir, tmp_path):  # noqa: F811
    root, ids = seeded(media_dir, tmp_path, "text")
    (tmp_path / "o.json").write_text(json.dumps(ops_for(ids, root)))
    out = runner.invoke(app, ["apply-ops", str(tmp_path / "o.json"), "--root", str(root), "--dry-run"])
    assert out.exit_code == 0 and "would apply 6 ops" in out.output
    out = runner.invoke(app, ["apply-ops", str(tmp_path / "o.json"), "--root", str(root)])
    assert out.exit_code == 0 and "applied 6 ops" in out.output
    shown = runner.invoke(app, ["compose", "show", NAME, "--root", str(root), "--json"])
    assert "titles" in [t["id"] for t in json.loads(shown.stdout)["tracks"]]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_returns_the_same_json_in_memory(media_dir, tmp_path, anyio_backend):  # noqa: F811
    engine_root, ids = seeded(media_dir, tmp_path, "engine")
    doc = ops_for(ids, engine_root)
    dry_expected = edit_ops.apply_ops(LocalMediaStore(engine_root), LocalProjectStore(engine_root), doc, dry_run=True)
    expected = edit_ops.apply_ops(LocalMediaStore(engine_root), LocalProjectStore(engine_root), doc)
    root, _ = seeded(media_dir, tmp_path, "mcp")
    async with Client(create_server(root)) as mcp:
        assert {"apply_ops", "get_edit_ops_schema"} <= {t.name for t in (await mcp.list_tools()).tools}
        assert await mcp_call(mcp, "get_edit_ops_schema") == edit_ops.schema()
        assert await mcp_call(mcp, "apply_ops", ops=doc, dry_run=True) == dry_expected
        assert LocalProjectStore(root).load_timeline(NAME).revision == 1
        bad = json.loads(json.dumps(doc))
        bad["ops"][1] = {"op": "remove_clip", "clip": "ghost"}
        failed = await mcp.call_tool("apply_ops", {"ops": bad})
        report = tool_error(failed)
        assert report["error"] == "ops_invalid" and report["errors"][0]["code"] == "ops.clip.not_found"
        assert report["errors"][0]["path"] == "/ops/1/clip" and report["applied"] == 1
        assert LocalProjectStore(root).load_timeline(NAME).revision == 1
        flagged = await mcp.call_tool("apply_ops", {"ops": bad, "dry_run": True, "continue_on_error": True})
        assert tool_error(flagged)["dry_run"] is True
        stale = await mcp.call_tool("apply_ops", {"ops": doc, "base_revision": 0})
        assert stale.is_error and tool_error(stale)["error"] == "revision_conflict"
        assert await mcp_call(mcp, "apply_ops", ops=doc, base_revision=1) == expected
        shown = await mcp_call(mcp, "show_timeline", name=NAME)
        assert shown["revision"] == 2 and "titles" in [t["id"] for t in shown["tracks"]]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_over_a_real_stdio_subprocess(media_dir, tmp_path, anyio_backend):  # noqa: F811
    engine_root, ids = seeded(media_dir, tmp_path, "engine")
    doc = ops_for(ids, engine_root)
    dry_expected = edit_ops.apply_ops(LocalMediaStore(engine_root), LocalProjectStore(engine_root), doc, dry_run=True)
    expected = edit_ops.apply_ops(LocalMediaStore(engine_root), LocalProjectStore(engine_root), doc)
    root, _ = seeded(media_dir, tmp_path, "stdio")
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(root)]
    )
    async with Client(params) as mcp:
        assert await mcp_call(mcp, "apply_ops", ops=doc, dry_run=True) == dry_expected
        assert LocalProjectStore(root).load_timeline(NAME).revision == 1
        assert await mcp_call(mcp, "apply_ops", ops=doc) == expected
        assert await mcp_call(mcp, "get_edit_ops_schema") == edit_ops.schema()
        failed = await mcp.call_tool("apply_ops", {"ops": {**doc, "ops": [{"op": "teleport"}]}})
        assert tool_error(failed)["errors"][0]["code"] == "ops.op.unknown"
    assert LocalProjectStore(root).load_existing_timeline(NAME) == LocalProjectStore(
        engine_root
    ).load_existing_timeline(NAME)
