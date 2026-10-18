import shutil

import pytest
from mcp import Client

from split_video.editor.classify import MODEL_PATH
from split_video.mcp_server import create_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def root(tmp_path, three_songs_clip):
    """A root directory holding one clip, plus a sibling file outside it."""
    shows = tmp_path / "shows"
    shows.mkdir()
    shutil.move(three_songs_clip, shows / "gig.mp4")
    (tmp_path / "secret.mp4").write_bytes(b"not a real video")
    return shows


async def call(client, tool, /, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def call_error(client, tool, /, **arguments):
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    return result.content[0].text


async def test_exposes_the_documented_tools(root):
    async with Client(create_server(root)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {
        "list_videos",
        "list_segments",
        "propose_splits",
        "add_split",
        "move_split",
        "remove_split",
        "label_segment",
        "analyze_audio",
        "export",
        "list_timelines",
        "create_timeline",
        "show_timeline",
        "add_clip_to_timeline",
        "trim_timeline_clip",
        "move_timeline_clip",
        "remove_timeline_clip",
        "set_timeline_transition",
        "render_timeline",
        "add_timeline_track",
        "remove_timeline_track",
        "move_timeline_track",
        "place_timeline_clip",
        "set_timeline_clip",
        "add_timeline_text",
        "set_timeline_text",
    }


async def test_list_videos_flags_which_have_saved_splits(root):
    async with Client(create_server(root)) as client:
        before = await call(client, "list_videos")
        assert before["entries"] == [{"path": "gig.mp4", "type": "video", "has_project": False}]
        await call(client, "add_split", video="gig.mp4", at=8)
        after = await call(client, "list_videos")
    assert after["entries"][0]["has_project"] is True


async def test_propose_only_shows_until_applied(root):
    async with Client(create_server(root)) as client:
        proposal = await call(client, "propose_splits", video="gig.mp4", min_song_length=2)
        assert len(proposal["segments"]) == 3 and proposal["applied"] is False
        assert (await call(client, "list_segments", video="gig.mp4"))["saved"] is False

        await call(client, "propose_splits", video="gig.mp4", min_song_length=2, apply=True)
        listed = await call(client, "list_segments", video="gig.mp4")
    assert listed["saved"] is True
    assert len(listed["segments"]) == 3


async def test_edit_tools_round_trip(root):
    async with Client(create_server(root)) as client:
        added = await call(client, "add_split", video="gig.mp4", at=8)
        assert added["split"] == 1

        moved = await call(client, "move_split", video="gig.mp4", split=1, to=10)
        assert moved["applied_to"] == 10

        labeled = await call(
            client, "label_segment", video="gig.mp4", segment=2, label="Encore", color="#ff8800", included=False
        )
        seg = labeled["segments"][1]
        assert (seg["label"], seg["color"], seg["included"]) == ("Encore", "#ff8800", False)

        removed = await call(client, "remove_split", video="gig.mp4", split=1)
    assert len(removed["segments"]) == 1


async def test_errors_come_back_as_readable_tool_errors(root):
    async with Client(create_server(root)) as client:
        assert "single-segment" in await call_error(client, "remove_split", video="gig.mp4", split=1)
        assert "at least one" in await call_error(client, "label_segment", video="gig.mp4", segment=1)
        assert "not found" in await call_error(client, "list_segments", video="nope.mp4")
        assert "#rrggbb" in await call_error(client, "label_segment", video="gig.mp4", segment=1, color="red")


@pytest.mark.parametrize("video", ["../secret.mp4", "/etc/passwd"])
async def test_paths_outside_the_root_are_refused(root, video):
    async with Client(create_server(root)) as client:
        message = await call_error(client, "list_segments", video=video)
        assert "outside" in message or "not found" in message
        # ...and nothing was written next to the file it pointed at.
        await call_error(client, "add_split", video=video, at=1)
    assert not (root.parent / "secret.mp4.split-video-project.json").exists()


async def test_list_videos_refuses_to_climb_out_of_the_root(root):
    async with Client(create_server(root)) as client:
        assert "outside" in await call_error(client, "list_videos", directory="..")


async def test_export_writes_included_segments_and_only_overrides(root):
    async with Client(create_server(root)) as client:
        await call(client, "propose_splits", video="gig.mp4", min_song_length=2, apply=True)
        await call(client, "label_segment", video="gig.mp4", segment=2, included=False, export_name="Middle")

        exported = await call(client, "export", video="gig.mp4")
        assert exported["files"] == ["gig_split/01 - gig.mp4", "gig_split/02 - gig.mp4"]
        assert exported["manifest"] == "gig_split/manifest.json"
        assert all((root / f).exists() for f in exported["files"])

        assert "already exist" in await call_error(client, "export", video="gig.mp4")

        only = await call(client, "export", video="gig.mp4", only=[2], overwrite=True)
    assert only["files"] == ["gig_split/Middle.mp4"]


async def test_mcp_edits_are_visible_to_the_cli(root):
    """The MCP server, CLI and editor all share one sidecar file."""
    import json

    from typer.testing import CliRunner

    from split_video.cli import app

    async with Client(create_server(root)) as client:
        await call(client, "add_split", video="gig.mp4", at=8)
        await call(client, "label_segment", video="gig.mp4", segment=1, label="Opener")

    result = CliRunner().invoke(app, ["segments", str(root / "gig.mp4"), "--json"])
    assert result.exit_code == 0
    first = json.loads(result.output)["segments"][0]
    assert (first["label"], first["end"]) == ("Opener", 8)


@pytest.mark.skipif(not MODEL_PATH.exists(), reason="YAMNet model not fetched (make fetch-model)")
async def test_analyze_audio_returns_a_summary(root):
    async with Client(create_server(root)) as client:
        summary = await call(client, "analyze_audio", video="gig.mp4")
    assert summary["analyzed"] is True


async def test_timeline_tools_build_and_render_a_two_clip_project(root):
    (root / "second.mp4").write_bytes((root / "gig.mp4").read_bytes())
    async with Client(create_server(root)) as client:
        await call(client, "create_timeline", name="mix")
        await call(client, "add_clip_to_timeline", name="mix", video="gig.mp4", out_point=6)
        body = await call(client, "add_clip_to_timeline", name="mix", video="second.mp4", out_point=4)
        clips = body["tracks"][0]["clips"]
        assert [c["index"] for c in clips] == [1, 2] and clips[1]["start"] == 6

        body = await call(client, "set_timeline_transition", name="mix", clip=2, transition="crossfade", duration=1)
        assert body["duration"] == 9
        body = await call(client, "trim_timeline_clip", name="mix", clip=1, in_point=1)
        assert body["duration"] == 8
        body = await call(client, "move_timeline_clip", name="mix", clip=2, to_position=1)
        assert body["tracks"][0]["clips"][0]["source"] == "second.mp4"

        assert (await call(client, "list_timelines"))["projects"] == ["mix"]
        rendered = await call(client, "render_timeline", name="mix")
        assert (root / rendered["output"]).is_file()
        assert "already exists" in await call_error(client, "render_timeline", name="mix")

        body = await call(client, "remove_timeline_clip", name="mix", clip=1)
        assert len(body["tracks"][0]["clips"]) == 1


async def test_timeline_tools_reject_bad_input(root):
    async with Client(create_server(root)) as client:
        await call(client, "create_timeline", name="mix")
        assert "outside" in await call_error(client, "add_clip_to_timeline", name="mix", video="../secret.mp4")
        assert "no clip 3" in await call_error(client, "remove_timeline_clip", name="mix", clip=3)
        assert "no project" in await call_error(client, "show_timeline", name="nope")


async def test_multi_track_tools_build_an_overlay_project(root):
    (root / "pip.mp4").write_bytes((root / "gig.mp4").read_bytes())
    async with Client(create_server(root)) as client:
        await call(client, "create_timeline", name="mix")
        await call(client, "add_clip_to_timeline", name="mix", video="gig.mp4", out_point=6)
        assert "no overlay video track" in await call_error(client, "add_timeline_text", name="mix", text="Hi")

        body = await call(client, "add_timeline_track", name="mix", kind="video")
        assert [t["index"] for t in body["tracks"]] == [1, 2] and body["tracks"][1]["magnetic"] is False
        body = await call(client, "add_clip_to_timeline", name="mix", video="pip.mp4", track=2, start=1, out_point=3)
        clip = body["tracks"][1]["clips"][0]
        assert (clip["start"], clip["duration"]) == (1, 3)

        body = await call(client, "set_timeline_clip", name="mix", clip=1, track=2, opacity=0.5, scale=0.3, muted=True)
        got = body["tracks"][1]["clips"][0]
        assert (got["opacity"], got["scale"], got["muted"]) == (0.5, 0.3, True)
        assert "at least one" in await call_error(client, "set_timeline_clip", name="mix", clip=1)

        body = await call(client, "place_timeline_clip", name="mix", clip=1, track=2, start=2)
        assert body["tracks"][1]["clips"][0]["start"] == 2
        assert "overlap" in await call_error(
            client, "add_clip_to_timeline", name="mix", video="pip.mp4", track=2, start=3, out_point=2
        )

        body = await call(client, "add_timeline_text", name="mix", text="Title", start=6, duration=2)
        assert body["tracks"][1]["clips"][1]["kind"] == "text"
        body = await call(client, "set_timeline_text", name="mix", clip=2, track=2, text="New", color="#ff0000")
        assert body["tracks"][1]["clips"][1]["text"] == "New"

        rendered = await call(client, "render_timeline", name="mix")
        assert (root / rendered["output"]).is_file()
        body = await call(client, "move_timeline_track", name="mix", track=2, to_position=1)
        assert body["tracks"][0]["magnetic"] is False
        body = await call(client, "remove_timeline_track", name="mix", track=1)
        assert len(body["tracks"]) == 1
