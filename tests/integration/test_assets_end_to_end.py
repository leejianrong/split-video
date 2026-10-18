"""Asset ids against real files: migrating old timelines, renames, and the API, CLI and MCP surfaces."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from split_video.cli import app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.ffmpeg import probe_duration
from split_video.mcp_server import create_server

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "v1_timeline.json"
TABLE = ".split-video-assets.json"
runner = CliRunner()


def _make(path, seconds, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:size=320x240:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path),
    ]  # fmt: skip
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def _cli(root, *args, ok=True):
    result = runner.invoke(app, ["compose", *[str(a) for a in args], "--root", str(root)])
    assert (result.exit_code == 0) == ok, result.output
    return result.output


def _cli_json(root, *args):
    return json.loads(_cli(root, *args, "--json"))


def _timeline_file(root, name):
    return json.loads((root / f"{name}.split-video-timeline.json").read_text())


def _table(root):
    return json.loads((root / TABLE).read_text())


@pytest.fixture
def root(tmp_path):
    _make(tmp_path / "a.mp4", 4, "red")
    _make(tmp_path / "sub" / "b.mp4", 4, "blue")
    return tmp_path


@pytest.fixture
def legacy(root):
    """A version 1 project (the shape `to_dict` wrote before asset ids) next to real videos; its third
    main-track clip names a file that does not exist yet."""
    shutil.copy(FIXTURE, root / "legacy.split-video-timeline.json")
    return root


# --- Done when: old projects load and gain assets automatically -----------------------------


def test_fixture_is_the_version_1_shape():
    data = json.loads(FIXTURE.read_text())
    clips = [c for t in data["tracks"] for c in t["clips"]]
    assert data["version"] == 1 and not any("asset" in c for c in clips)
    assert "magnetic" not in data["tracks"][2] and any(c["kind"] == "text" for c in clips)


def test_a_version_1_project_loads_gains_assets_and_is_not_rewritten_by_reading(legacy):
    before = (legacy / "legacy.split-video-timeline.json").read_text()
    shown = _cli_json(legacy, "show", "legacy")
    clips = {c["id"]: c for t in shown["tracks"] for c in t["clips"]}

    assert clips["c1"]["asset"] == clips["o1"]["asset"] == clips["s1"]["asset"]  # one file, one asset
    assert (
        clips["c2"]["asset"] and clips["c2"]["asset"] != clips["c1"]["asset"] and clips["c2"]["source"] == "sub/b.mp4"
    )
    assert clips["c3"]["asset"] == "" and clips["c3"]["source"] == "missing.mp4"  # kept, unlinked
    assert clips["t1"]["kind"] == "text" and clips["t1"]["asset"] == ""
    assert shown["tracks"][2]["magnetic"] is True  # the track without the flag

    table = _table(legacy)["assets"]
    assert sorted(row["path"] for row in table.values()) == ["a.mp4", "sub/b.mp4"]
    assert table[clips["c1"]["asset"]]["has_audio"] and table[clips["c1"]["asset"]]["width"] == 320
    assert table[clips["c1"]["asset"]]["fps"] == 25.0
    assert (legacy / "legacy.split-video-timeline.json").read_text() == before


def test_the_first_edit_saves_the_project_as_version_2(legacy):
    _cli(legacy, "remove", "legacy", 3)  # drop the clip whose file doesn't exist
    saved = _timeline_file(legacy, "legacy")
    assert saved["version"] == 2
    media_clips = [c for t in saved["tracks"] for c in t["clips"] if c["kind"] == "media"]
    assert media_clips and all(c["asset"].startswith("a_") and c["source"] for c in media_clips)
    assert not any("asset" in c for t in saved["tracks"] for c in t["clips"] if c["kind"] == "text")
    assert all("magnetic" in t for t in saved["tracks"])


def test_a_migrated_project_renders_with_text_overlay_and_audio_tracks(legacy):
    _cli(legacy, "remove", "legacy", 3)
    _cli(legacy, "render", "legacy")
    # main: a 0..2 then b 1..3 back to back; the overlay and audio clips fit inside it
    assert probe_duration(legacy / "legacy-render.mp4") == pytest.approx(4.0, abs=0.3)


def test_a_missing_source_is_a_clear_error_until_the_file_reappears(legacy):
    out = _cli(legacy, "render", "legacy", ok=False)
    assert "missing.mp4" in out and "not found" in out

    _make(legacy / "missing.mp4", 3, "green")
    shown = _cli_json(legacy, "show", "legacy")
    assert shown["tracks"][0]["clips"][2]["asset"].startswith("a_")  # linked once it resolves
    _cli(legacy, "render", "legacy")
    assert probe_duration(legacy / "legacy-render.mp4") == pytest.approx(5.5, abs=0.3)


def test_migrate_command_rewrites_the_file_now(legacy):
    _make(legacy / "missing.mp4", 3, "green")
    assert _timeline_file(legacy, "legacy")["version"] == 1
    _cli(legacy, "migrate", "legacy")
    saved = _timeline_file(legacy, "legacy")
    assert saved["version"] == 2
    assert all(c["asset"] for t in saved["tracks"] for c in t["clips"] if c["kind"] == "media")


# --- Done when: renaming a source does not break a re-linked clip ---------------------------------


def test_a_renamed_source_is_relinked_by_id_and_still_renders(root):
    _cli(root, "new", "show")
    _cli(root, "add-clip", "show", "a.mp4", "--out", 2)
    _cli(root, "add-clip", "show", "sub/b.mp4", "--out", 2)
    asset = _cli_json(root, "show", "show")["tracks"][0]["clips"][0]["asset"]
    (root / "a.mp4").rename(root / "renamed.mp4")

    clip = _cli_json(root, "show", "show")["tracks"][0]["clips"][0]
    assert (
        clip["asset"] == asset
        and clip["source"] == "renamed.mp4"
        and clip["source_duration"] == pytest.approx(4, abs=0.2)
    )
    assert _timeline_file(root, "show")["tracks"][0]["clips"][0]["source"] == "a.mp4"  # reading doesn't rewrite
    assert _table(root)["assets"][asset]["path"] == "renamed.mp4"  # but the table is updated

    _cli(root, "render", "show")
    assert probe_duration(root / "show-render.mp4") == pytest.approx(4.0, abs=0.3)

    _cli(root, "set", "show", 1, "--gain", 0.5)  # the next edit refreshes the clip's hint
    saved = _timeline_file(root, "show")["tracks"][0]["clips"][0]
    assert saved["source"] == "renamed.mp4" and saved["asset"] == asset


def test_same_bytes_at_a_new_path_is_found_by_id(root):
    _cli(root, "new", "show")
    _cli(root, "add-clip", "show", "a.mp4", "--out", 2)
    asset = _cli_json(root, "show", "show")["tracks"][0]["clips"][0]["asset"]
    (root / "elsewhere").mkdir()
    shutil.move(root / "a.mp4", root / "elsewhere" / "other-name.mp4")

    clip = _cli_json(root, "show", "show")["tracks"][0]["clips"][0]
    assert clip["asset"] == asset and clip["source"] == "elsewhere/other-name.mp4"
    _cli(root, "render", "show")

    # a byte-identical copy is the same asset, and the table keeps its first path
    shutil.copy(root / "elsewhere" / "other-name.mp4", root / "copy.mp4")
    _cli(root, "add-clip", "show", "copy.mp4", "--out", 1)
    clips = _cli_json(root, "show", "show")["tracks"][0]["clips"]
    assert clips[1]["asset"] == asset and clips[1]["source"] == "copy.mp4"
    assert list(_table(root)["assets"]) == [asset]


def test_an_asset_whose_file_is_gone_is_an_error_naming_the_id_and_path(root):
    _cli(root, "new", "show")
    _cli(root, "add-clip", "show", "a.mp4")
    asset = _cli_json(root, "show", "show")["tracks"][0]["clips"][0]["asset"]
    (root / "a.mp4").unlink()
    out = _cli(root, "render", "show", ok=False)
    assert asset in out and "a.mp4" in out


# --- Done when: CLI accepts asset ids -----------------------------------------------------------------


def test_cli_add_clip_takes_a_path_or_an_asset_id_and_lists_assets(root):
    _cli(root, "new", "show")
    _cli(root, "add-clip", "show", "a.mp4", "--out", 2)
    asset = _cli_json(root, "assets")["assets"][0]
    assert asset["path"] == "a.mp4" and asset["present"] and asset["id"].startswith("a_")

    shown = _cli_json(root, "add-clip", "show", "--asset", asset["id"], "--in", 1, "--out", 3)
    assert shown["tracks"][0]["clips"][1]["asset"] == asset["id"]
    assert asset["id"] in _cli(root, "show", "show")
    assert asset["id"] in _cli(root, "assets")

    assert "not both" in _cli(root, "add-clip", "show", "a.mp4", "--asset", asset["id"], ok=False)
    assert "not both" in _cli(root, "add-clip", "show", ok=False)
    assert "unknown asset" in _cli(root, "add-clip", "show", "--asset", "a_0123456789abcdef", ok=False)


def test_cli_assets_with_none_registered(tmp_path):
    assert "No assets" in _cli(tmp_path, "assets")
    assert _cli_json(tmp_path, "assets") == {"assets": []}


# --- Done when: the HTTP API accepts asset ids --------------------------------------------------------


@pytest.fixture
def client(root):
    return TestClient(create_app(root, DEFAULTS))


BASE = "/api/v1/compose/projects/show"


def test_api_add_clip_by_path_or_asset_and_clip_output_has_both(client):
    client.post("/api/v1/compose/projects", json={"name": "show"})
    body = client.post(f"{BASE}/clips", json={"path": "a.mp4", "out_point": 2}).json()
    clip = body["tracks"][0]["clips"][0]
    assert clip["asset"].startswith("a_") and clip["source"] == "a.mp4"

    body = client.post(f"{BASE}/clips", json={"asset": clip["asset"], "in_point": 1, "out_point": 3}).json()
    assert body["tracks"][0]["clips"][1]["asset"] == clip["asset"]

    assert client.post(f"{BASE}/clips", json={}).status_code == 422
    assert client.post(f"{BASE}/clips", json={"path": "a.mp4", "asset": clip["asset"]}).status_code == 422
    assert client.post(f"{BASE}/clips", json={"asset": "a_0123456789abcdef"}).status_code == 404


def test_api_assets_endpoints(client, root):
    assert client.get("/api/v1/assets").json() == {"assets": []}
    registered = client.post("/api/v1/assets/register", json={"path": "sub/b.mp4"})
    assert registered.status_code == 200
    asset = registered.json()
    assert asset["path"] == "sub/b.mp4" and asset["present"] and asset["width"] == 320 and asset["fps"] == 25.0
    assert client.post("/api/v1/assets/register", json={"path": "sub/b.mp4"}).json() == asset  # idempotent
    assert client.get("/api/v1/assets").json() == {"assets": [asset]}
    assert client.get(f"/api/v1/assets/{asset['id']}").json() == asset

    assert client.post("/api/v1/assets/register", json={"path": "nope.mp4"}).status_code == 404
    assert client.post("/api/v1/assets/register", json={"path": "../x.mp4"}).status_code == 400
    assert client.get("/api/v1/assets/a_0123456789abcdef").status_code == 404

    (root / "sub" / "b.mp4").rename(root / "moved.mp4")
    moved = client.get(f"/api/v1/assets/{asset['id']}").json()
    assert moved["path"] == "moved.mp4" and moved["present"]
    (root / "moved.mp4").unlink()
    gone = client.get(f"/api/v1/assets/{asset['id']}")
    assert gone.status_code == 200 and gone.json()["present"] is False


def test_api_serves_media_by_asset_id_with_ranges_and_keeps_the_path_route(client, root):
    asset = client.post("/api/v1/assets/register", json={"path": "a.mp4"}).json()
    whole = client.get(f"/api/v1/media/asset/{asset['id']}")
    assert whole.status_code == 200 and whole.content == (root / "a.mp4").read_bytes()
    part = client.get(f"/api/v1/media/asset/{asset['id']}", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert client.get("/api/v1/media/asset/a_0123456789abcdef").status_code == 404
    assert client.get("/api/v1/media/file", params={"path": "a.mp4"}).status_code == 200

    (root / "a.mp4").rename(root / "renamed.mp4")  # still served: found by id
    assert client.get(f"/api/v1/media/asset/{asset['id']}").status_code == 200


def test_api_loads_a_version_1_project_and_the_first_edit_saves_version_2(legacy, client):
    path = legacy / "legacy.split-video-timeline.json"
    before = path.read_text()
    body = client.get("/api/v1/compose/projects/legacy").json()
    clips = {c["id"]: c for t in body["tracks"] for c in t["clips"]}
    assert (
        clips["c1"]["asset"].startswith("a_") and clips["c3"]["asset"] == "" and clips["c3"]["source_duration"] is None
    )
    assert clips["c2"]["end"] > clips["c2"]["start"] > 0  # laid out, not collapsed to zero
    assert path.read_text() == before

    client.patch("/api/v1/compose/projects/legacy/clips/c1/properties", json={"gain": 0.5})
    saved = json.loads(path.read_text())
    assert saved["version"] == 2 and saved["tracks"][0]["clips"][0]["asset"] == clips["c1"]["asset"]


def test_api_render_finds_a_renamed_source(client, root):
    client.post("/api/v1/compose/projects", json={"name": "show"})
    client.post(f"{BASE}/clips", json={"path": "a.mp4", "out_point": 2})
    (root / "a.mp4").rename(root / "renamed.mp4")
    job = client.post(f"{BASE}/render", json={}).json()["job_id"]
    for _ in range(100):
        status = client.get(f"/api/v1/compose/render/{job}").json()
        if status["status"] != "running":
            break
        time.sleep(0.2)
    assert status["status"] == "done", status
    assert (root / status["output"]).is_file()


# --- Done when: MCP accepts asset ids -------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(client, tool, /, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.anyio
async def test_mcp_add_clip_by_asset_and_list_assets(root):
    async with Client(create_server(root)) as client:
        assert (await _call(client, "list_assets"))["assets"] == []
        await _call(client, "create_timeline", name="mix")
        body = await _call(client, "add_clip_to_timeline", name="mix", video="a.mp4", out_point=2)
        clip = body["tracks"][0]["clips"][0]
        assert clip["asset"].startswith("a_") and clip["source"] == "a.mp4"

        listed = (await _call(client, "list_assets"))["assets"]
        assert [(a["id"], a["path"], a["present"]) for a in listed] == [(clip["asset"], "a.mp4", True)]

        body = await _call(client, "add_clip_to_timeline", name="mix", asset=clip["asset"], in_point=1, out_point=3)
        assert body["tracks"][0]["clips"][1]["asset"] == clip["asset"]

        for arguments in ({}, {"video": "a.mp4", "asset": clip["asset"]}):
            result = await client.call_tool("add_clip_to_timeline", {"name": "mix", **arguments})
            assert result.is_error and "exactly one" in result.content[0].text

        (root / "a.mp4").rename(root / "renamed.mp4")
        shown = await _call(client, "show_timeline", name="mix")
        assert shown["tracks"][0]["clips"][0]["source"] == "renamed.mp4"
        rendered = await _call(client, "render_timeline", name="mix")
        assert (root / rendered["output"]).is_file()
