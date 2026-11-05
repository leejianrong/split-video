"""/api/v1 is the whole public surface; the pre-versioning /api and /media paths are gone."""

import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient

from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_export_script():
    spec = importlib.util.spec_from_file_location("export_openapi", REPO_ROOT / "scripts" / "export_openapi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_route_is_under_api_v1(tmp_path):
    app = create_app(tmp_path, DEFAULTS)
    paths = app.openapi()["paths"]
    assert len(paths) > 20
    assert all(p.startswith("/api/v1/") for p in paths), [p for p in paths if not p.startswith("/api/v1/")]


def test_committed_openapi_json_matches_the_app():
    script = _load_export_script()
    committed = (REPO_ROOT / "openapi.json").read_text()
    assert committed == script.render_schema(), "openapi.json is stale; run `make openapi`"
    assert json.loads(committed)["paths"]


def test_pre_versioning_paths_are_gone(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    params = {"path": three_songs_clip.name}
    assert client.get("/api/v1/state", params=params).json()["video_url"].startswith("/api/v1/media/source?")
    for old in ("/api/state", "/api/session", "/api/compose/projects", "/media/source", "/media/file"):
        assert client.get(old, params=params).status_code == 404, old


def test_unknown_v1_path_is_404_not_shadowed(tmp_path):
    client = TestClient(create_app(tmp_path, DEFAULTS))
    assert client.get("/api/v1/nope").status_code == 404
