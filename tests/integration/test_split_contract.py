"""The split surfaces' contract, pinned (SPL-116).

Scripted sessions on the CLI (`--json` and table output), the MCP tools and the HTTP routes, replayed against golden
outputs that were recorded on the code from BEFORE the timeline project became the one model
(`tests/fixtures/split_contract/golden.json`). They run on a fresh video and on real legacy split files
(`tests/fixtures/split_legacy/`). The tests look only at what the surfaces return, never at which file holds the
project, so they must pass unchanged however it is stored.

Regenerate (only for a deliberate contract change, and list the change in the PR):
    UPDATE_GOLDEN=1 uv run pytest tests/integration/test_split_contract.py
"""

import json
import os
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from split_video.cli import app as cli_app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.mcp_server import create_server

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
LEGACY = FIXTURES / "split_legacy"
GOLDEN = FIXTURES / "split_contract" / "golden.json"
VARIANTS = ["fresh", "v1_minimal", "v2_labelled", "v2_empty", "v2_unparseable", "v2_gaps_and_overlap"]
DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
runner = CliRunner()


# --- helpers ---------------------------------------------------------------------------------


def place(tmp_path: Path, clip: Path, variant: str) -> Path:
    """A root holding `gig.mp4` (and its legacy split file, if the variant has one); returns the root."""
    root = tmp_path / "shows"
    root.mkdir()
    shutil.copy(clip, root / "gig.mp4")
    if variant != "fresh":
        shutil.copy(LEGACY / f"{variant}.split-video-project.json", root / "gig.mp4.split-video-project.json")
    return root


def clean(value, root: Path):
    """JSON-ready, with the temp root masked and floats rounded to a millisecond."""
    if isinstance(value, float):
        return round(value, 3) + 0.0
    if isinstance(value, dict):
        return {k: clean(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v, root) for v in value]
    if isinstance(value, str):
        return value.replace(str(root.resolve()), "<ROOT>").replace(str(root), "<ROOT>")
    return value


def cli(*args) -> object:
    return runner.invoke(cli_app, [str(a) for a in args])


def cli_step(root: Path, *args) -> dict:
    """Run one CLI command with `--json`; the exit code plus the parsed JSON (or the raw output if it isn't)."""
    result = cli(*args, "--json")
    try:
        body = json.loads(result.output)
    except ValueError:
        body = result.output
    return clean({"exit": result.exit_code, "out": body}, root)


def table_step(root: Path, *args) -> dict:
    result = cli(*args)
    return clean({"exit": result.exit_code, "out": result.output}, root)


def compare(name: str, actual: dict) -> None:
    """Match `actual` to the golden entry `name` (or record it when UPDATE_GOLDEN is set)."""
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if os.environ.get("UPDATE_GOLDEN"):
        golden[name] = actual
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")
        return
    assert name in golden, f"no golden for {name}: record it with UPDATE_GOLDEN=1 on the pre-change code"
    expected = golden[name]
    assert sorted(actual) == sorted(expected), "the steps differ"
    for step in expected:
        assert actual[step] == expected[step], f"step {step!r} changed"


# --- CLI -------------------------------------------------------------------------------------


def cli_session(root: Path) -> dict:
    video = root / "gig.mp4"
    steps: dict[str, dict] = {}

    def run(label, *args):
        steps[label] = cli_step(root, *args)

    run("segments", "segments", video)
    steps["segments_table"] = table_step(root, "segments", video)
    run("add_split", "add-split", video, "--at", "0:07.5")
    run("add_split_again", "add-split", video, "--at", "15")
    run("add_split_too_close", "add-split", video, "--at", "7.52")
    run("add_split_outside", "add-split", video, "--at", "0")
    run("add_split_bad_time", "add-split", video, "--at", "nonsense")
    run("move_split", "move-split", video, 1, "--to", "9")
    run("move_split_clamped", "move-split", video, 1, "--to", "99")
    run("move_split_missing", "move-split", video, 9, "--to", "3")
    run("label", "label", video, 2, "--label", " Encore ", "--color", "#FF8800", "--exclude", "--export-name", "Mid")
    run("label_clear", "label", video, 2, "--color", "", "--export-name", "", "--include")
    run("label_bad_color", "label", video, 2, "--color", "red")
    run("label_missing_segment", "label", video, 99, "--label", "x")
    run("label_nothing", "label", video, 2)
    run("remove_split", "remove-split", video, 1)
    run("remove_split_missing", "remove-split", video, 7)
    run("stale_edit", "label", video, 1, "--label", "stale", "--base-revision", 0)
    run("segments_after", "segments", video)
    run(
        "fresh_base_revision_ok",
        "label",
        video,
        1,
        "--label",
        "kept",
        "--base-revision",
        steps["segments_after"]["out"]["revision"],
    )
    run("label_for_export", "label", video, 1, "--export-name", "Part One")
    run("export_only", "export", video, "--only", "2", "--format", "mp4", "--no-manifest")
    run("export_default", "export", video, "--overwrite")
    run("export_conflict", "export", video)
    run("export_bad_only", "export", video, "--only", "9", "--overwrite")
    run("propose", "propose", video, "--min-song-length", 2)
    run("propose_apply", "propose", video, "--min-song-length", 2, "--apply")
    run("propose_stale", "propose", video, "--min-song-length", 2, "--apply", "--base-revision", 0)
    run("segments_final", "segments", video)
    steps["table_final"] = table_step(root, "segments", video)
    # file lists only (the manifest has a timestamp)
    steps["exported_files"] = {"files": sorted(p.name for p in (root / "gig_split").iterdir())}
    return steps


@pytest.mark.parametrize("variant", VARIANTS)
def test_cli_contract(three_songs_clip, tmp_path, variant):
    root = place(tmp_path, three_songs_clip, variant)
    compare(f"cli/{variant}", cli_session(root))


# --- MCP -------------------------------------------------------------------------------------


async def tool(client: Client, root: Path, tool_name: str, /, **arguments) -> dict:
    result = await client.call_tool(tool_name, arguments)
    if result.is_error:
        return clean({"error": result.content[0].text}, root)
    return clean({"ok": result.structured_content}, root)


async def mcp_session(root: Path) -> dict:
    steps: dict[str, dict] = {}
    async with Client(create_server(root)) as client:

        async def run(step, tool_name, /, **arguments):
            steps[step] = await tool(client, root, tool_name, **arguments)

        await run("list_videos", "list_videos")
        await run("list_segments", "list_segments", video="gig.mp4")
        await run("add_split", "add_split", video="gig.mp4", at=7.5)
        await run("add_split_outside", "add_split", video="gig.mp4", at=0.0)
        await run("add_split_again", "add_split", video="gig.mp4", at=15.0)
        await run("move_split", "move_split", video="gig.mp4", split=1, to=9.0)
        await run("move_split_clamped", "move_split", video="gig.mp4", split=1, to=99.0)
        await run("move_split_missing", "move_split", video="gig.mp4", split=9, to=3.0)
        await run("label", "label_segment", video="gig.mp4", segment=2, label="Encore", color="#FF8800", included=False)
        await run("label_clear", "label_segment", video="gig.mp4", segment=2, color="", export_name="", included=True)
        await run("label_bad_color", "label_segment", video="gig.mp4", segment=2, color="red")
        await run("label_nothing", "label_segment", video="gig.mp4", segment=2)
        await run("remove_split", "remove_split", video="gig.mp4", split=1)
        await run("remove_split_missing", "remove_split", video="gig.mp4", split=7)
        await run("stale_edit", "label_segment", video="gig.mp4", segment=1, label="stale", base_revision=0)
        await run("list_after", "list_segments", video="gig.mp4")
        await run("list_videos_after", "list_videos")
        revision = steps["list_after"]["ok"]["revision"]
        await run(
            "fresh_base_revision_ok", "label_segment", video="gig.mp4", segment=1, label="kept", base_revision=revision
        )
        await run("label_for_export", "label_segment", video="gig.mp4", segment=1, export_name="Part One")
        await run("export_only", "export", video="gig.mp4", only=[2], manifest=False)
        await run("export_default", "export", video="gig.mp4", overwrite=True)
        await run("export_conflict", "export", video="gig.mp4")
        await run("export_bad_only", "export", video="gig.mp4", only=[9], overwrite=True)
        await run("propose_preview", "propose_splits", video="gig.mp4", min_song_length=2.0)
        await run("propose_apply", "propose_splits", video="gig.mp4", min_song_length=2.0, apply=True)
        await run("propose_stale", "propose_splits", video="gig.mp4", min_song_length=2.0, apply=True, base_revision=0)
        await run("list_final", "list_segments", video="gig.mp4")
        await run("missing_video", "list_segments", video="nope.mp4")
        await run("outside_root", "list_segments", video="../secret.mp4")
    steps["exported_files"] = {"files": sorted(p.name for p in (root / "gig_split").iterdir())}
    return steps


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("variant", VARIANTS)
async def test_mcp_contract(three_songs_clip, tmp_path, variant):
    root = place(tmp_path, three_songs_clip, variant)
    compare(f"mcp/{variant}", await mcp_session(root))


# --- HTTP ------------------------------------------------------------------------------------


def http_session(root: Path) -> dict:
    client = TestClient(create_app(root, DEFAULTS))
    path = {"path": "gig.mp4"}
    steps: dict[str, dict] = {}

    def record(label, response):
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
        # the video url carries nothing machine-specific; params are the server defaults
        steps[label] = clean({"status": response.status_code, "body": body}, root)

    def if_match(revision):
        return {"If-Match": f'"{revision}"'}

    record("state", client.get("/api/v1/state", params=path))
    record("open", client.post("/api/v1/open", json=path))
    revision = steps["state"]["body"]["revision"]
    first = {
        "segments": [
            {"start": 0.0, "end": 6.0, "label": " keep "},
            {"start": 6.0, "end": 14.0, "label": "mid", "color": "#aabbcc", "included": False, "export_name": "Mid"},
            {"start": 14.0, "end": 20.5},
        ]
    }
    record("save", client.post("/api/v1/project", params=path, json=first, headers=if_match(revision)))
    record("state_after_save", client.get("/api/v1/state", params=path))
    record("save_stale", client.post("/api/v1/project", params=path, json=first, headers=if_match(revision)))
    saved_revision = steps["save"]["body"]["revision"]
    record("save_no_if_match", client.post("/api/v1/project", params=path, json=first))
    record(
        "save_bare_integer",
        client.post("/api/v1/project", params=path, json=first, headers={"If-Match": str(saved_revision + 1)}),
    )
    record("save_bad_if_match", client.post("/api/v1/project", params=path, json=first, headers={"If-Match": "abc"}))
    record("save_empty", client.post("/api/v1/project", params=path, json={"segments": []}))
    record("state_after_empty", client.get("/api/v1/state", params=path))
    record(
        "save_gaps",
        client.post(
            "/api/v1/project",
            params=path,
            json={"segments": [{"start": 1.0, "end": 3.0}, {"start": 5.0, "end": 9.0}, {"start": 8.0, "end": 12.0}]},
        ),
    )
    record(
        "save_invalid_body", client.post("/api/v1/project", params=path, json={"segments": [{"start": "x", "end": 1}]})
    )
    record("state_missing_file", client.get("/api/v1/state", params={"path": "nope.mp4"}))
    record("state_escape", client.get("/api/v1/state", params={"path": "../x.mp4"}))
    body = {"silences": [{"start": 7.0, "end": 10.0}, {"start": 15.0, "end": 18.0}], "duration": 21.0}
    body |= {"min_silence_duration": 2.0, "min_song_length": 2.0, "padding": 0.15}
    record("segments_from_silences", client.post("/api/v1/segments", json=body))
    return steps


@pytest.mark.parametrize("variant", VARIANTS)
def test_http_contract(three_songs_clip, tmp_path, variant):
    root = place(tmp_path, three_songs_clip, variant)
    compare(f"http/{variant}", http_session(root))


def test_nothing_machine_specific_in_the_golden_file():
    if not GOLDEN.exists():
        pytest.skip("not recorded yet")
    assert not re.search(r"/tmp/|/home/|pytest-of", GOLDEN.read_text())
