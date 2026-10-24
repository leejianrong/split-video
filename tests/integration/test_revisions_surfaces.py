"""Stale writes through every front door: HTTP 409, CLI exit 3 and MCP error, each with the current revision."""

import json

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from split_video.cli import app as cli_app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()


def cli(*args):
    return runner.invoke(cli_app, [str(a) for a in args])


def cli_json(*args):
    result = cli(*args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def root(three_songs_clip, two_songs_clip):
    return three_songs_clip.parent


@pytest.fixture
def client(root):
    return TestClient(create_app(root, DEFAULTS))


def if_match(revision):
    return {"If-Match": f'"{revision}"'}


# --- HTTP API ----------------------------------------------------------------------------


def test_split_project_state_and_saves_carry_the_revision(client, three_songs_clip):
    path = {"path": three_songs_clip.name}
    assert client.get("/api/v1/state", params=path).json()["revision"] == 0
    segs = {"segments": [{"start": 0.0, "end": 6.0}, {"start": 6.0, "end": 20.0}]}
    saved = client.post("/api/v1/project", params=path, json=segs, headers=if_match(0))
    assert saved.status_code == 200 and saved.json()["revision"] == 1
    assert client.post("/api/v1/open", json=path).json()["revision"] == 1
    # Without If-Match the save applies to the latest version; a bare integer works too.
    assert client.post("/api/v1/project", params=path, json=segs).json()["revision"] == 2
    assert client.post("/api/v1/project", params=path, json=segs, headers={"If-Match": "2"}).json()["revision"] == 3


def test_stale_split_save_is_409_with_the_current_revision_and_writes_nothing(client, three_songs_clip):
    path = {"path": three_songs_clip.name}
    mine = {"segments": [{"start": 0.0, "end": 20.0, "label": "mine"}]}
    theirs = {"segments": [{"start": 0.0, "end": 20.0, "label": "theirs"}]}
    assert client.post("/api/v1/project", params=path, json=theirs, headers=if_match(0)).status_code == 200
    stale = client.post("/api/v1/project", params=path, json=mine, headers=if_match(0))
    assert stale.status_code == 409
    body = stale.json()
    assert {"detail", "error", "current_revision", "base_revision"} <= set(body)
    assert body["code"] == "revision.conflict" and body["fix"] and body["details"]["current_revision"] == 1
    assert (body["error"], body["current_revision"], body["base_revision"]) == ("revision_conflict", 1, 0)
    assert "retry" in body["detail"] and "base_revision=1" in body["detail"] and "If-Match" in body["detail"]
    state = client.get("/api/v1/state", params=path).json()
    assert [s["label"] for s in state["segments"]] == ["theirs"] and state["revision"] == 1


def test_state_reflects_a_change_made_by_another_process_behind_the_servers_back(client, three_songs_clip):
    path = {"path": three_songs_clip.name}
    client.post("/api/v1/open", json=path)  # the server has the project loaded
    assert cli("add-split", three_songs_clip, "--at", 8).exit_code == 0  # ...then the CLI edits the sidecar
    state = client.get("/api/v1/state", params=path).json()
    assert state["revision"] == 1 and len(state["segments"]) == 2
    stale = client.post(
        "/api/v1/project", params=path, json={"segments": [{"start": 0.0, "end": 20.0}]}, headers=if_match(0)
    )
    assert stale.status_code == 409 and stale.json()["current_revision"] == 1


def test_a_bad_if_match_is_a_400(client, three_songs_clip):
    res = client.post(
        "/api/v1/project",
        params={"path": three_songs_clip.name},
        json={"segments": [{"start": 0.0, "end": 1.0}]},
        headers={"If-Match": "banana"},
    )
    assert res.status_code == 400


def _timeline(client, name="show"):
    assert client.post("/api/v1/compose/projects", json={"name": name}).json()["revision"] == 0
    return f"/api/v1/compose/projects/{name}"


def test_timeline_writes_carry_the_revision_and_stale_ones_409(client, three_songs_clip, two_songs_clip):
    base = _timeline(client)
    one = client.post(f"{base}/clips", json={"path": three_songs_clip.name}, headers=if_match(0))
    assert one.status_code == 200 and one.json()["revision"] == 1
    clip_id = one.json()["tracks"][0]["clips"][0]["id"]
    assert client.get(base).json()["revision"] == 1

    # Someone else edits: revision 2. Our PATCH, still based on 1, is refused.
    other = client.post(f"{base}/tracks", json={"kind": "audio"}, headers=if_match(1))
    assert other.json()["revision"] == 2
    stale = client.patch(f"{base}/clips/{clip_id}", json={"in_point": 1.0}, headers=if_match(1))
    assert stale.status_code == 409
    assert {k: stale.json()[k] for k in ("error", "current_revision", "base_revision")} == {
        "error": "revision_conflict",
        "current_revision": 2,
        "base_revision": 1,
    }
    assert stale.json()["code"] == "revision.conflict" and "If-Match: 2" in stale.json()["fix"]
    assert "base_revision=2" in stale.json()["detail"]
    assert client.get(base).json()["tracks"][0]["clips"][0]["in_point"] == 0  # not applied

    # The same for DELETE, which has no body: the header is the one mechanism for every write.
    stale_delete = client.delete(f"{base}/clips/{clip_id}", headers=if_match(1))
    assert stale_delete.status_code == 409 and stale_delete.json()["current_revision"] == 2
    assert len(client.get(base).json()["tracks"][0]["clips"]) == 1

    # Retrying with the revision from the conflict works, and reports the next one.
    deleted = client.delete(f"{base}/clips/{clip_id}", headers=if_match(2))
    assert deleted.status_code == 200 and deleted.json()["revision"] == 3
    assert deleted.json()["tracks"][0]["clips"] == []


def test_every_timeline_write_route_is_conditional(client, three_songs_clip):
    base = _timeline(client)
    added = client.post(f"{base}/clips", json={"path": three_songs_clip.name}).json()  # no header: latest
    clip_id = added["tracks"][0]["clips"][0]["id"]
    track_id = added["tracks"][0]["id"]
    stale = if_match(0)
    writes = [
        ("post", f"{base}/clips", {"path": three_songs_clip.name}),
        ("patch", f"{base}/clips/{clip_id}", {"in_point": 1.0}),
        ("post", f"{base}/tracks", {"kind": "audio"}),
        ("delete", f"{base}/tracks/{track_id}", None),
        ("post", f"{base}/tracks/{track_id}/move", {"to_index": 0}),
        ("patch", f"{base}/clips/{clip_id}/properties", {"gain": 0.5}),
        ("post", f"{base}/tracks/{track_id}/text", {"text": "hi"}),
        ("patch", f"{base}/clips/{clip_id}/text", {"text": "x"}),
        ("post", f"{base}/clips/{clip_id}/place", {"start": 1.0}),
        ("post", f"{base}/clips/{clip_id}/move", {"to_index": 0}),
        ("put", f"{base}/clips/{clip_id}/transition", {"transition": "cut"}),
        ("delete", f"{base}/clips/{clip_id}", None),
    ]
    for method, url, body in writes:
        kwargs = {"headers": stale, **({} if body is None else {"json": body})}
        res = getattr(client, method)(url, **kwargs)
        assert res.status_code == 409, (method, url, res.text)
        assert res.json()["error"] == "revision_conflict" and res.json()["current_revision"] == 1
    assert client.get(base).json()["revision"] == 1  # nothing got through


def test_other_409s_are_distinguishable_from_revision_conflicts(client):
    _timeline(client)
    dup = client.post("/api/v1/compose/projects", json={"name": "show"})
    assert dup.status_code == 409 and "error" not in dup.json()


def test_openapi_documents_if_match_and_the_409_on_write_routes(client):
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/api/v1/compose/projects/{name}/clips/{clip_id}"]["delete"]
    assert any(p["name"] == "If-Match" and p["in"] == "header" for p in op["parameters"])
    assert op["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].endswith("RevisionConflictOut")
    assert "revision" in schema["components"]["schemas"]["ProjectOut"]["properties"]
    assert "revision" in schema["components"]["schemas"]["StateResponse"]["properties"]


# --- CLI -------------------------------------------------------------------------------------


def test_cli_split_commands_show_and_check_revisions(three_songs_clip):
    assert cli_json("segments", three_songs_clip)["revision"] == 0
    first = cli_json("add-split", three_songs_clip, "--at", 6, "--base-revision", 0)
    assert first["revision"] == 1
    cli_json("add-split", three_songs_clip, "--at", 12)  # no base: applies to the latest
    assert cli_json("segments", three_songs_clip)["revision"] == 2

    stale = cli("move-split", three_songs_clip, 1, "--to", 7, "--base-revision", 1)
    assert stale.exit_code == 3
    assert "revision 2" in stale.output and "revision 1" in stale.output
    assert "segments" in stale.output and "--base-revision 2" in stale.output  # how to retry
    assert len(cli_json("segments", three_songs_clip)["segments"]) == 3  # unchanged
    assert cli_json("move-split", three_songs_clip, 1, "--to", 7, "--base-revision", 2)["revision"] == 3
    assert "revision 3" in cli("segments", three_songs_clip).output


def test_cli_split_conflict_with_json_prints_a_machine_readable_error(three_songs_clip):
    cli_json("add-split", three_songs_clip, "--at", 6)
    stale = runner.invoke(
        cli_app, ["label", str(three_songs_clip), "1", "--label", "x", "--base-revision", "0", "--json"]
    )
    assert stale.exit_code == 3
    body = json.loads(stale.stdout[stale.stdout.index("{") :])
    assert (body["error"], body["current_revision"], body["base_revision"]) == ("revision_conflict", 1, 0)


def test_cli_propose_apply_checks_the_base_revision(three_songs_clip):
    cli_json("add-split", three_songs_clip, "--at", 6)
    stale = cli("propose", three_songs_clip, "--min-song-length", 2, "--apply", "--base-revision", 0)
    assert stale.exit_code == 3 and "--base-revision 1" in stale.output
    assert cli_json("segments", three_songs_clip)["revision"] == 1


def test_cli_compose_edits_show_revision_and_refuse_stale_ones(root, three_songs_clip):
    r = ["--root", root]
    assert cli_json("compose", "new", "mix", *r)["revision"] == 0
    assert cli_json("compose", "add-clip", "mix", three_songs_clip.name, *r)["revision"] == 1
    assert cli_json("compose", "add-track", "mix", "--kind", "audio", *r)["revision"] == 2
    assert "revision 2" in cli("compose", "show", "mix", *r).output

    stale = cli("compose", "trim", "mix", 1, "--in", 1, "--base-revision", 1, *r)
    assert stale.exit_code == 3
    assert "revision 2" in stale.output and "compose show mix" in stale.output and "--base-revision 2" in stale.output
    assert cli_json("compose", "show", "mix", *r)["tracks"][0]["clips"][0]["in"] == 0  # unchanged

    stale_json = cli("compose", "set", "mix", 1, "--gain", 0.5, "--base-revision", 0, "--json", *r)
    assert stale_json.exit_code == 3
    assert json.loads(stale_json.stdout[stale_json.stdout.index("{") :])["current_revision"] == 2

    retried = cli_json("compose", "trim", "mix", 1, "--in", 1, "--base-revision", 2, *r)
    assert retried["revision"] == 3 and retried["tracks"][0]["clips"][0]["in"] == 1


def test_every_cli_edit_command_takes_base_revision():
    from split_video import compose_cli, project_cli

    editing = {
        project_cli: ["propose_cmd", "add_split_cmd", "move_split_cmd", "remove_split_cmd", "label_cmd"],
        compose_cli: [
            "add_clip_cmd", "trim_cmd", "move_cmd", "remove_cmd", "transition_cmd", "add_track_cmd",
            "remove_track_cmd", "move_track_cmd", "place_cmd", "set_cmd", "add_text_cmd", "set_text_cmd",
            "migrate_cmd",
        ],
    }  # fmt: skip
    for module, names in editing.items():
        for name in names:
            assert "base_revision" in getattr(module, name).__annotations__, name


# --- MCP ---------------------------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(client, tool, /, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def _error(client, tool, /, **arguments):
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    text = result.content[0].text  # "Error executing tool <name>: " then the JSON object
    assert text.startswith("Error executing tool")
    return json.loads(text[text.index("{") :])


@pytest.mark.anyio
async def test_mcp_split_conflict_is_structured_with_retry_guidance(root, three_songs_clip):
    async with Client(create_server(root)) as client:
        listed = await _call(client, "list_segments", video=three_songs_clip.name)
        assert listed["revision"] == 0
        added = await _call(client, "add_split", video=three_songs_clip.name, at=6, base_revision=0)
        assert added["revision"] == 1
        error = await _error(client, "add_split", video=three_songs_clip.name, at=12, base_revision=0)
        assert (error["error"], error["current_revision"], error["base_revision"]) == ("revision_conflict", 1, 0)
        assert "base_revision=1" in error["message"] and "list_segments" in error["fix"]
        assert len((await _call(client, "list_segments", video=three_songs_clip.name))["segments"]) == 2
        retried = await _call(client, "add_split", video=three_songs_clip.name, at=12, base_revision=1)
        assert retried["revision"] == 2
        latest = await _call(client, "label_segment", video=three_songs_clip.name, segment=1, label="x")
        assert latest["revision"] == 3  # no base_revision: applied to the latest


@pytest.mark.anyio
async def test_mcp_timeline_conflict_and_revisions_in_results(root, three_songs_clip):
    async with Client(create_server(root)) as client:
        assert (await _call(client, "create_timeline", name="mix"))["revision"] == 0
        added = await _call(client, "add_clip_to_timeline", name="mix", video=three_songs_clip.name, base_revision=0)
        assert added["revision"] == 1
        assert (await _call(client, "show_timeline", name="mix"))["revision"] == 1
        cli("compose", "add-track", "mix", "--kind", "audio", "--root", root)  # another writer: revision 2

        error = await _error(client, "trim_timeline_clip", name="mix", clip=1, in_point=1, base_revision=1)
        assert (error["error"], error["current_revision"], error["base_revision"]) == ("revision_conflict", 2, 1)
        assert "base_revision=2" in error["message"] and "show_timeline" in error["fix"]
        error = await _error(client, "add_timeline_track", name="mix", base_revision=0)
        assert error["current_revision"] == 2
        trimmed = await _call(client, "trim_timeline_clip", name="mix", clip=1, in_point=1, base_revision=2)
        assert trimmed["revision"] == 3 and trimmed["tracks"][0]["clips"][0]["in"] == 1


@pytest.mark.anyio
async def test_mcp_editing_tools_all_accept_base_revision(root):
    async with Client(create_server(root)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    editing = [
        "propose_splits", "add_split", "move_split", "remove_split", "label_segment", "add_clip_to_timeline",
        "trim_timeline_clip", "move_timeline_clip", "remove_timeline_clip", "set_timeline_transition",
        "add_timeline_track", "remove_timeline_track", "move_timeline_track", "place_timeline_clip",
        "set_timeline_clip", "add_timeline_text", "set_timeline_text",
    ]  # fmt: skip
    for name in editing:
        assert "base_revision" in tools[name].input_schema["properties"], name
