"""Writes into a folder the editor cannot write to are one catalogued 403 on every surface, never a bare 500.

A read-only folder is a `chmod 0555` one; root bypasses permissions, so these tests are skipped for root."""

import json
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from split_video.cli import app as cli_app
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.mcp_server import create_server

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root can write into a read-only folder")

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
CODE = "folder.not_writable"


@pytest.fixture
def workspace(tmp_path, three_songs_clip):
    root = tmp_path / "ws"
    root.mkdir()
    shutil.copy(three_songs_clip, root / "a.mp4")
    return root


@pytest.fixture
def read_only(workspace):
    """Make the workspace read-only once the test has set its state up: call the returned function."""

    def lock():
        workspace.chmod(0o555)

    yield lock
    workspace.chmod(0o755)


def _assert_403(response):
    body = response.json()
    assert response.status_code == 403, body
    assert body["code"] == CODE and "chown" in body["fix"]
    assert str(os.sep) not in body["message"]  # no absolute path from the OS error


def test_saving_a_timeline_project_is_a_403(workspace, read_only):
    client = TestClient(create_app(workspace, DEFAULTS), raise_server_exceptions=False)
    assert client.post("/api/v1/compose/projects", json={"name": "show"}).status_code == 200
    read_only()
    _assert_403(client.post("/api/v1/compose/projects", json={"name": "other"}))
    _assert_403(client.post("/api/v1/compose/projects/show/clips", json={"path": "a.mp4"}))


def test_writing_the_undo_history_is_a_403(workspace, read_only):
    client = TestClient(create_app(workspace, DEFAULTS), raise_server_exceptions=False)
    client.post("/api/v1/compose/projects", json={"name": "show"})
    client.post("/api/v1/compose/projects/show/clips", json={"path": "a.mp4"})
    read_only()
    _assert_403(client.post("/api/v1/compose/projects/show/undo"))


def test_saving_split_points_is_a_403(workspace, read_only):
    client = TestClient(create_app(workspace, DEFAULTS), raise_server_exceptions=False)
    client.post("/api/v1/assets/register", json={"path": "a.mp4"})
    read_only()
    _assert_403(client.post("/api/v1/project", params={"path": "a.mp4"}, json={"segments": []}))


def test_writing_a_derived_cache_fails_the_job_with_the_code(workspace, read_only):
    client = TestClient(create_app(workspace, DEFAULTS), raise_server_exceptions=False)
    asset = client.post("/api/v1/assets/register", json={"path": "a.mp4"}).json()["id"]
    read_only()
    job_id = client.post(f"/api/v1/assets/{asset}/waveform").json()["job_id"]
    deadline = time.time() + 30
    while (job := client.get(f"/api/v1/jobs/{job_id}").json())["state"] not in ("done", "error"):
        assert time.time() < deadline
        time.sleep(0.05)
    assert job["state"] == "error" and job["error_info"]["code"] == CODE


def test_the_cli_reports_the_same_code_and_fix(workspace, read_only):
    read_only()
    result = CliRunner().invoke(cli_app, ["--json-errors", "compose", "new", "show", "--root", str(workspace)])
    assert result.exit_code == 1 and "Traceback" not in result.output
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["code"] == CODE and "chown" in payload["fix"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_mcp_reports_the_same_code_and_fix(workspace, read_only, anyio_backend):
    read_only()
    async with Client(create_server(workspace)) as client:
        result = await client.call_tool("create_timeline", {"name": "show"})
    assert result.is_error
    text = result.content[0].text
    payload = json.loads(text[text.index("{") :])
    assert payload["code"] == CODE and "chown" in payload["fix"]
