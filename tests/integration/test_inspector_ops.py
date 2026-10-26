"""The contextual inspector's edits (SPL-66): every control is one `set_clip_properties` / `set_text_properties` op on
the recording's own timeline. Each changes the clip as `GET /project/timeline` reports it, bumps the revision by one,
and an invalid value changes nothing."""

import pytest
from fastapi.testclient import TestClient
from test_plan_surfaces import (
    DEFAULTS,
    media_dir,  # noqa: F401  (a fixture)
    workspace,
)

from split_video import project
from split_video.editor.app import create_app
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.store import SavedSegment

VIDEO = "green.mp4"


@pytest.fixture
def client(media_dir, tmp_path):  # noqa: F811
    root, _ = workspace(media_dir, tmp_path, "inspector")
    store, media = LocalProjectStore(root), LocalMediaStore(root)
    project.save_segments(
        store, VIDEO, [SavedSegment(0, 2, label="Opening"), SavedSegment(2, 6, label="Song 1")], media=media
    )
    return TestClient(create_app(root, DEFAULTS))


def timeline(client):
    got = client.get("/api/v1/project/timeline", params={"path": VIDEO})
    assert got.status_code == 200, got.text
    return got.json()


def apply(client, ops, revision):
    return client.post(
        "/api/v1/project/ops",
        params={"path": VIDEO},
        json={"schema_version": 1, "ops": ops},
        headers={"If-Match": f'"{revision}"'},
    )


def first_clip(data):
    return data["tracks"][0]["clips"][0]


@pytest.mark.parametrize(
    "properties",
    [
        {"gain": 0.5012},
        {"muted": True},
        {"brightness": 0.2, "contrast": 1.5, "saturation": 0.5},
        {"scale": 0.5, "x": 0.25, "y": 0.75, "opacity": 0.5},
        {"crop": {"x": 0.3418, "y": 0.0, "w": 0.3164, "h": 1.0}},
    ],
)
def test_each_inspector_control_edits_the_clip_and_bumps_the_revision(client, properties):
    before = timeline(client)
    clip = first_clip(before)
    got = apply(client, [{"op": "set_clip_properties", "clip": clip["id"], **properties}], before["revision"])
    assert got.status_code == 200, got.text
    after = timeline(client)
    assert after["revision"] == before["revision"] + 1
    for key, value in properties.items():
        assert first_clip(after)[key] == value
    # reading it back (a page reload) shows the same state, and the other clip is untouched
    assert after["tracks"][0]["clips"][1]["gain"] == before["tracks"][0]["clips"][1]["gain"]


def test_reset_restores_the_defaults_and_removes_the_crop(client):
    data = timeline(client)
    clip = first_clip(data)
    changed = apply(
        client,
        [
            {
                "op": "set_clip_properties",
                "clip": clip["id"],
                "gain": 2,
                "brightness": 0.5,
                "crop": {"x": 0, "y": 0, "w": 0.5, "h": 0.5},
            }
        ],
        data["revision"],
    )
    assert changed.status_code == 200
    data = timeline(client)
    reset = {
        "gain": 1,
        "muted": False,
        "x": 0.5,
        "y": 0.5,
        "scale": 1,
        "opacity": 1,
        "brightness": 0,
        "contrast": 1,
        "saturation": 1,
        "crop": None,
    }
    assert (
        apply(client, [{"op": "set_clip_properties", "clip": clip["id"], **reset}], data["revision"]).status_code == 200
    )
    after = first_clip(timeline(client))
    assert after["gain"] == 1 and after["brightness"] == 0 and after["crop"] is None


def test_several_clips_change_in_one_atomic_batch(client):
    data = timeline(client)
    ops = [{"op": "set_clip_properties", "clip": c["id"], "gain": 2} for c in data["tracks"][0]["clips"]]
    assert apply(client, ops, data["revision"]).status_code == 200
    after = timeline(client)
    assert [c["gain"] for c in after["tracks"][0]["clips"]] == [2, 2]
    assert after["revision"] == data["revision"] + 1  # one batch, one revision


def test_an_invalid_value_changes_nothing(client):
    data = timeline(client)
    clip = first_clip(data)
    for bad in ({"gain": 9}, {"crop": {"x": 0.9, "y": 0, "w": 0.5, "h": 1}}):
        got = apply(client, [{"op": "set_clip_properties", "clip": clip["id"], **bad}], data["revision"])
        assert got.status_code == 422, got.text
    assert timeline(client) == data


def test_a_stale_revision_is_a_conflict(client):
    data = timeline(client)
    clip = first_clip(data)
    assert (
        apply(client, [{"op": "set_clip_properties", "clip": clip["id"], "muted": True}], data["revision"]).status_code
        == 200
    )
    stale = apply(client, [{"op": "set_clip_properties", "clip": clip["id"], "muted": False}], data["revision"])
    assert stale.status_code == 409
    assert first_clip(timeline(client))["muted"] is True


def test_a_title_is_styled_through_text_properties(client):
    data = timeline(client)
    ops = [
        {"op": "add_track", "id": "titles", "kind": "video"},
        {"op": "add_text_clip", "id": "cap", "track": "titles", "text": "Hello", "duration": 2},
    ]
    assert apply(client, ops, data["revision"]).status_code == 200
    data = timeline(client)
    edit = [
        {"op": "set_text_properties", "clip": "cap", "text": "Hi", "font_size": 0.1, "color": "#ffcc00"},
        {"op": "set_clip_properties", "clip": "cap", "x": 0.3, "y": 0.2},
    ]
    assert apply(client, edit, data["revision"]).status_code == 200
    clips = [c for t in timeline(client)["tracks"] for c in t["clips"] if c["id"] == "cap"]
    assert clips[0]["text"] == "Hi" and clips[0]["font_size"] == 0.1
    assert clips[0]["color"] == "#ffcc00" and clips[0]["x"] == 0.3 and clips[0]["y"] == 0.2
