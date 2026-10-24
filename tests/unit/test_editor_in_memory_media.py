"""The editor app served entirely from in-memory stores: no media on disk."""

import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from split_video import project
from split_video.editor import cache as cache_module
from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams
from split_video.ffmpeg import MediaInfo
from split_video.media_store import InMemoryMediaStore
from split_video.silence import SilenceInterval
from split_video.store import InMemoryProjectStore

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
DATA = bytes(range(256)) * 4  # 1024 bytes


@pytest.fixture
def media():
    store = InMemoryMediaStore()
    store.add("gig.mp4", DATA, info=MediaInfo(60.0, 320, 240, True))
    store.add("sub/other.mp4", b"x" * 10, info=MediaInfo(5.0, 320, 240, True))
    return store


@pytest.fixture
def client(tmp_path, media):
    return TestClient(create_app(tmp_path, DEFAULTS, store=InMemoryProjectStore(), media=media))


def test_browse_lists_the_media_store_and_rejects_escapes(client):
    body = client.get("/api/v1/browse").json()
    assert [(e["name"], e["is_dir"]) for e in body["entries"]] == [("sub", True), ("gig.mp4", False)]
    assert client.get("/api/v1/browse", params={"path": "sub"}).json()["entries"][0]["path"] == "sub/other.mp4"
    assert client.get("/api/v1/browse", params={"path": "../"}).status_code == 400
    assert client.get("/api/v1/browse", params={"path": "nope"}).status_code == 404


def test_state_save_and_resume_use_the_stores(client, tmp_path):
    state = client.get("/api/v1/state", params={"path": "gig.mp4"}).json()
    assert state["duration"] == 60.0 and state["filename"] == "gig.mp4" and not state["resumed"]
    saved = client.post(
        "/api/v1/project",
        params={"path": "gig.mp4"},
        json={"segments": [{"start": 0, "end": 30}, {"start": 30, "end": 60}]},
    ).json()
    assert saved["resumed"] is True
    assert client.post("/api/v1/open", json={"path": "gig.mp4"}).json()["segments"][1]["start"] == 30
    assert client.get("/api/v1/state", params={"path": "missing.mp4"}).status_code == 404
    assert client.get("/api/v1/state", params={"path": "../x.mp4"}).status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_detect_and_waveform_cache_through_the_media_store(client, media, monkeypatch):
    scans = []
    monkeypatch.setattr(
        cache_module, "detect_silence", lambda *a, **k: scans.append(a[1]) or [SilenceInterval(start=1.0, end=3.0)]
    )
    monkeypatch.setattr(
        cache_module, "iter_pcm_chunks", lambda *a, **k: (c for c in [np.array([100, -100, 50, 0], dtype="<i2")])
    )
    for _ in range(2):
        assert client.post("/api/v1/detect", params={"path": "gig.mp4"}, json={"silence_threshold": -35.0}).json() == {
            "silences": [{"start": 1.0, "end": 3.0}]
        }
    assert len(scans) == 1
    assert client.get("/api/v1/waveform", params={"path": "gig.mp4"}).json()["buckets"]
    assert media.load_cache("gig.mp4", "silence") is not None and media.load_cache("gig.mp4", "waveform") is not None


def test_media_source_serves_ranges_from_open_read(client):
    full = client.get("/api/v1/media/source", params={"path": "gig.mp4"})
    assert full.status_code == 200 and full.content == DATA
    assert full.headers["accept-ranges"] == "bytes" and full.headers["content-type"] == "video/mp4"

    part = client.get("/api/v1/media/source", params={"path": "gig.mp4"}, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == DATA[100:200]
    assert part.headers["content-range"] == "bytes 100-199/1024"

    tail = client.get("/api/v1/media/source", params={"path": "gig.mp4"}, headers={"Range": "bytes=1000-"})
    assert tail.status_code == 206 and tail.content == DATA[1000:]
    last = client.get("/api/v1/media/source", params={"path": "gig.mp4"}, headers={"Range": "bytes=-24"})
    assert last.status_code == 206 and last.content == DATA[-24:]
    clamped = client.get("/api/v1/media/source", params={"path": "gig.mp4"}, headers={"Range": "bytes=1000-9999"})
    assert clamped.headers["content-range"] == "bytes 1000-1023/1024"

    beyond = client.get("/api/v1/media/source", params={"path": "gig.mp4"}, headers={"Range": "bytes=2000-"})
    assert beyond.status_code == 416 and beyond.headers["content-range"] == "bytes */1024"
    assert client.get("/api/v1/media/file", params={"path": "sub/other.mp4"}).content == b"x" * 10
    assert client.get("/api/v1/media/file", params={"path": "../x"}).status_code == 400
    assert client.get("/api/v1/media/file", params={"path": "nope.mp4"}).status_code == 404
    assert client.get("/media/source", params={"path": "gig.mp4"}).headers["deprecation"] == "true"


def test_export_job_writes_to_the_media_store(client, media, monkeypatch):
    monkeypatch.setattr(project, "extract_segment", lambda *a, **k: None)
    started = client.post(
        "/api/v1/export",
        params={"path": "gig.mp4"},
        json={"segments": [{"start": 0, "end": 30, "name": None}, {"start": 30, "end": 60, "name": "Encore"}]},
    )
    assert started.status_code == 200
    job = started.json()["job_id"]
    for _ in range(50):
        status = client.get(f"/api/v1/export/{job}").json()
        if status["status"] != "running":
            break
        time.sleep(0.05)
    assert status["status"] == "done" and status["output_dir"] == "memory:gig_split"
    assert media.exists("gig_split/Encore.mp4") and media.exists("gig_split/manifest.json")
    again = client.post(
        "/api/v1/export", params={"path": "gig.mp4"}, json={"segments": [{"start": 0, "end": 30, "name": "Encore"}]}
    )
    assert again.status_code == 409


def test_compose_runs_against_the_media_store(client):
    assert client.post("/api/v1/compose/projects", json={"name": "show"}).status_code == 200
    added = client.post("/api/v1/compose/projects/show/clips", json={"path": "gig.mp4"})
    assert added.status_code == 200
    clip = added.json()["tracks"][0]["clips"][0]
    assert clip["source"] == "gig.mp4" and clip["source_duration"] == 60.0
    assert client.post("/api/v1/compose/projects/show/clips", json={"path": "../x.mp4"}).status_code == 400
    assert client.post("/api/v1/compose/projects/show/clips", json={"path": "nope.mp4"}).status_code == 404
