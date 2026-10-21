"""/api/v1 is the whole public surface; old paths are deprecated aliases."""

import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient

from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
# Routes added after versioning shipped never had an unversioned spelling, so they get no alias.
NO_LEGACY_ALIAS = {
    "/api/v1/events",
    "/api/v1/whoami",
    "/api/v1/compose/render-queue",
    "/api/v1/compose/render-queue/{batch_id}",
    "/api/v1/compose/render-queue/{batch_id}/cancel",
    "/api/v1/presets",
    "/api/v1/caption-styles",
    "/api/v1/library/scan",
    "/api/v1/assets/{asset_id}/proxy",
    "/api/v1/assets/{asset_id}/thumbnails",
    "/api/v1/assets/{asset_id}/thumbnails/info",
    "/api/v1/assets/{asset_id}/transcribe",
    "/api/v1/assets/{asset_id}/transcript",
    "/api/v1/assets/{asset_id}/transcript/search",
    "/api/v1/assets/{asset_id}/scenes",
    "/api/v1/assets/{asset_id}/ocr",
    "/api/v1/assets/{asset_id}/ocr/search",
    "/api/v1/assets/{asset_id}/search",
    "/api/v1/assets/{asset_id}/search/visual",
    "/api/v1/assets/{asset_id}/at",
    "/api/v1/assets/{asset_id}/timeline",
    "/api/v1/assets/{asset_id}/report",
    "/api/v1/assets/{asset_id}/report/prepare",
    "/api/v1/assets/{asset_id}/highlights",
    "/api/v1/assets/{asset_id}/frame",
    "/api/v1/assets/{asset_id}/reframe",
    "/api/v1/assets/{asset_id}/contact-sheet",
    "/api/v1/assets/{asset_id}/contact-sheet/info",
    "/api/v1/compose/projects/{name}/ops",
    "/api/v1/compose/projects/{name}/plan",
    "/api/v1/schemas/edit-ops",
    "/api/v1/schemas/edit-plan",
}
REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_export_script():
    spec = importlib.util.spec_from_file_location("export_openapi", REPO_ROOT / "scripts" / "export_openapi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operations(app):
    for path, methods in app.openapi()["paths"].items():
        for method in methods:
            yield path, method


def test_every_route_is_under_api_v1(tmp_path):
    app = create_app(tmp_path, DEFAULTS)
    paths = app.openapi()["paths"]
    assert len(paths) > 20
    assert all(p.startswith("/api/v1/") for p in paths), [p for p in paths if not p.startswith("/api/v1/")]
    # The schema lists no legacy alias either.
    assert not any(p.startswith("/api/") and not p.startswith("/api/v1/") for p in paths)
    assert "/media/source" not in paths and "/media/file" not in paths


def test_every_v1_operation_has_a_deprecated_alias(tmp_path):
    client = TestClient(create_app(tmp_path, DEFAULTS))
    for path, method in _operations(client.app):
        if path in NO_LEGACY_ALIAS:
            continue
        legacy = path.removeprefix("/api/v1")
        if legacy.startswith("/media/"):
            pass  # /media/... keeps its pre-versioning spelling
        else:
            legacy = "/api" + legacy
        legacy = legacy.replace("{job_id}", "x").replace("{name}", "x").replace("{track_id}", "x")
        legacy = legacy.replace("{clip_id}", "x").replace("{asset_id}", "x")
        response = client.request(method, legacy)
        # A route exists if we get anything but the router's bare 404/405.
        assert response.status_code != 405, (method, legacy)
        if response.status_code == 404:
            assert response.json().get("detail") != "Not Found", (method, legacy)
        assert response.headers.get("deprecation") == "true", (method, legacy)


def test_committed_openapi_json_matches_the_app():
    script = _load_export_script()
    committed = (REPO_ROOT / "openapi.json").read_text()
    assert committed == script.render_schema(), "openapi.json is stale; run `make openapi`"
    assert json.loads(committed)["paths"]


def test_old_paths_still_work_and_are_marked_deprecated(three_songs_clip):
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS))
    params = {"path": three_songs_clip.name}

    old = client.get("/api/state", params=params)
    new = client.get("/api/v1/state", params=params)
    assert old.status_code == new.status_code == 200
    assert old.json() == new.json()
    assert old.headers["deprecation"] == "true"
    assert "deprecation" not in new.headers

    # video_url in the state points at the versioned media route.
    assert new.json()["video_url"].startswith("/api/v1/media/source?")
    assert client.get("/media/source", params=params).status_code == 200
    assert client.get("/api/v1/media/source", params=params).status_code == 200
    assert client.get("/api/session").status_code == 200
    assert client.get("/api/compose/projects").json() == {"projects": []}
    assert client.get("/media/file", params={"path": three_songs_clip.name}).status_code == 200


def test_unknown_v1_path_is_404_not_shadowed(tmp_path):
    client = TestClient(create_app(tmp_path, DEFAULTS))
    assert client.get("/api/v1/nope").status_code == 404
