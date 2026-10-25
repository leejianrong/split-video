"""The built Svelte app is served at /app/ next to the old pages (docs/adr/0004-frontend-stack-and-structure.md)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from split_video.editor import app as app_module
from split_video.editor.app import APP_NOT_BUILT_HINT, create_app
from split_video.editor.schemas import StateParams

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.fixture
def built_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "static_app"
    (out / "assets").mkdir(parents=True)
    (out / "index.html").write_text('<!doctype html><title>shell</title><script src="/app/assets/index-abc123.js">')
    (out / "assets" / "index-abc123.js").write_text("console.log('hi')")
    monkeypatch.setattr(app_module, "STATIC_APP_DIR", out)
    return out


def _client(tmp_path: Path) -> TestClient:
    root = tmp_path / "videos"
    root.mkdir()
    return TestClient(create_app(root, DEFAULTS))


def test_app_is_served_when_built(built_app: Path, tmp_path: Path) -> None:
    client = _client(tmp_path)
    for path in ("/app/", "/app/index.html"):
        response = client.get(path)
        assert response.status_code == 200
        assert "<title>shell</title>" in response.text
    assert client.get("/app", follow_redirects=False).status_code in (301, 307)


def test_index_is_no_store_and_hashed_assets_are_immutable(built_app: Path, tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/app/").headers["cache-control"] == "no-store"
    assert client.get("/app/index.html").headers["cache-control"] == "no-store"
    asset = client.get("/app/assets/index-abc123.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == IMMUTABLE
    # A missing hashed file must not get cached forever.
    assert client.get("/app/assets/missing.js").headers["cache-control"] == "no-store"


def test_old_pages_are_unchanged_by_the_app_mount(built_app: Path, tmp_path: Path) -> None:
    client = _client(tmp_path)
    static = Path(app_module.__file__).parent / "static"
    for url, name in (("/", "index.html"), ("/compose.html", "compose.html")):
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == (static / name).read_bytes()
        assert response.headers["cache-control"] == "no-store"


def test_missing_build_returns_404_with_hint_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "static_app"
    monkeypatch.setattr(app_module, "STATIC_APP_DIR", missing)
    client = _client(tmp_path)
    for path in ("/app", "/app/", "/app/assets/index.js"):
        response = client.get(path)
        assert response.status_code == 404
        assert APP_NOT_BUILT_HINT in response.text
        assert "npm run build" in response.text
    assert not missing.exists()
    assert client.get("/").status_code == 200  # the old pages still work


def test_api_is_not_shadowed(built_app: Path, tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/v1/whoami").status_code != 404
    assert "/app" not in str(client.get("/openapi.json").json().get("paths", {}).keys())
