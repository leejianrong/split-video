"""Edit ops on a recording's own timeline (SPL-65) through the engine, HTTP, the CLI and MCP: the same JSON comes out of
every surface, `show` gives the clip ids and revision the ops need, and a stale revision is refused everywhere."""

import json

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

from split_video import edit_ops, project
from split_video.cli import app
from split_video.editor.app import create_app
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.mcp_server import create_server
from split_video.store import SavedSegment

runner = CliRunner()
VIDEO = "green.mp4"  # 6 s with audio


def seeded(media_dir, tmp_path, name):  # noqa: F811
    root, _ = workspace(media_dir, tmp_path, name)
    store, media = LocalProjectStore(root), LocalMediaStore(root)
    project.save_segments(
        store, VIDEO, [SavedSegment(0, 2, label="Opening"), SavedSegment(2, 6, label="Song 1")], media=media
    )
    return root, store, media


def doc_for(timeline):
    track, first, second = timeline.tracks[0], timeline.tracks[0].clips[0], timeline.tracks[0].clips[1]
    return {
        "schema_version": 1,
        "ops": [
            {"op": "trim_clip", "clip": second.id, "out": 5.5},
            {"op": "move_clip", "clip": first.id, "to_index": 1},
            {"op": "set_track_flags", "track": track.id, "muted": True},
        ],
    }


def test_the_same_result_from_the_engine_http_and_cli(media_dir, tmp_path):  # noqa: F811
    results = {}
    for surface in ("engine", "http", "cli"):
        root, store, media = seeded(media_dir, tmp_path, surface)
        timeline = project.video_timeline(store, media, VIDEO)
        revision = project.current_revision(store, VIDEO)
        doc = doc_for(timeline)
        if surface == "engine":
            results[surface] = edit_ops.apply_ops(
                media,
                store,
                edit_ops.with_project(doc, VIDEO),
                base_revision=revision,
                target=project.video_ops_target(store, media, VIDEO),
            )
        elif surface == "http":
            client = TestClient(create_app(root, DEFAULTS))
            got = client.post(
                "/api/v1/project/ops", params={"path": VIDEO}, json=doc, headers={"If-Match": f'"{revision}"'}
            )
            assert got.status_code == 200, got.text
            results[surface] = got.json()
        else:
            path = tmp_path / "ops.json"
            path.write_text(json.dumps(doc))
            shown = runner.invoke(app, ["compose", "show", "--video", VIDEO, "--root", str(root), "--json"])
            assert shown.exit_code == 0, shown.output
            assert json.loads(shown.stdout)["revision"] == revision
            out = runner.invoke(
                app,
                [
                    "apply-ops",
                    str(path),
                    "--video",
                    VIDEO,
                    "--root",
                    str(root),
                    "--base-revision",
                    str(revision),
                    "--json",
                ],
            )
            assert out.exit_code == 0, out.output
            results[surface] = json.loads(out.stdout)
        saved = project.video_timeline(store, media, VIDEO)
        assert [c.label for c in saved.tracks[0].clips] == ["Song 1", "Opening"]
        assert saved.tracks[0].muted and saved.tracks[0].clips[0].out_point == 5.5
        assert project.current_revision(store, VIDEO) == revision + 1
    # the clip ids differ per workspace (random for the first save), so compare the shape and the rest
    for result in results.values():
        result.pop("ops_hash")
        for entry in result["results"]:
            entry.pop("ids")
        for track in result["tracks"]:
            track.pop("id")
        assert result["project"] == VIDEO
    assert results["engine"] == results["http"] == results["cli"]


def test_cli_refuses_a_stale_revision_and_a_video_that_is_not_there(media_dir, tmp_path):  # noqa: F811
    root, store, media = seeded(media_dir, tmp_path, "cli")
    doc = doc_for(project.video_timeline(store, media, VIDEO))
    path = tmp_path / "ops.json"
    path.write_text(json.dumps(doc))
    stale = runner.invoke(
        app, ["apply-ops", str(path), "--video", VIDEO, "--root", str(root), "--base-revision", "0", "--json"]
    )
    assert stale.exit_code == 3
    missing = runner.invoke(app, ["apply-ops", str(path), "--video", "nope.mp4", "--root", str(root), "--json"])
    assert missing.exit_code != 0
    both = runner.invoke(app, ["compose", "show", "reel", "--video", VIDEO, "--root", str(root)])
    assert both.exit_code != 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_edits_the_video_timeline_and_shows_it(media_dir, tmp_path, anyio_backend):  # noqa: F811
    root, store, media = seeded(media_dir, tmp_path, "mcp")
    async with Client(create_server(root)) as mcp:
        shown = await mcp_call(mcp, "show_timeline", video=VIDEO)
        assert shown["revision"] == project.current_revision(store, VIDEO)
        clips = shown["tracks"][0]["clips"]
        assert [c["id"] for c in clips] == [c.id for c in project.video_timeline(store, media, VIDEO).tracks[0].clips]
        doc = {
            "schema_version": 1,
            "ops": [{"op": "trim_clip", "clip": clips[1]["id"], "out": 5.5}],
        }
        dry = await mcp_call(mcp, "apply_ops", ops=doc, video=VIDEO, dry_run=True, base_revision=shown["revision"])
        assert dry["dry_run"] and dry["timeline"]["tracks"][0]["clips"][1]["out"] == 5.5
        assert project.current_revision(store, VIDEO) == shown["revision"]
        done = await mcp_call(mcp, "apply_ops", ops=doc, video=VIDEO, base_revision=shown["revision"])
        assert done["revision"] == shown["revision"] + 1 and done["project"] == VIDEO
        stale = await mcp.call_tool("apply_ops", {"ops": doc, "video": VIDEO, "base_revision": shown["revision"]})
        assert tool_error(stale)["error"] == "revision_conflict"
        wrong = await mcp.call_tool("apply_ops", {"ops": {**doc, "project": "other.mp4"}, "video": VIDEO})
        assert tool_error(wrong)["errors"][0]["code"] == "ops.project.mismatch"
        neither = await mcp.call_tool("show_timeline", {})
        assert neither.is_error
