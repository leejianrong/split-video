"""The project canvas (SPL-82): stored in the project file, set by the `set_canvas` op, shown with the clips that no
longer match it, and the default of renders and previews that name no preset or size."""

import json

import pytest
from test_edit_plan import World

from split_video import edit_ops, presets, timeline, timeline_service
from split_video.edit_ops import OpsInvalidError
from split_video.errors import RevisionConflictError

NAME = "reel"


@pytest.fixture(params=["memory", "local"])
def world(request, tmp_path):
    return World(request.param, tmp_path)


def set_canvas(w, ratio, **kw):
    doc = {"schema_version": 1, "project": NAME, "ops": [{"op": "set_canvas", "ratio": ratio}]}
    return edit_ops.apply_ops(w.media, w.store, doc, **kw)


def seeded(w):
    timeline_service.create_project(w.store, NAME)
    timeline_service.add_clip(w.store, w.media, NAME, asset=w.ids["a"], in_point=0, out_point=30)


def view(w):
    return timeline_service.describe(w.media, NAME, w.store.load_timeline(NAME))


def test_a_file_without_a_canvas_loads_as_16_9_and_writes_none_back():
    data = {"version": 2, "tracks": [{"id": "t", "kind": "video", "clips": []}]}
    project = timeline.from_dict(data)
    assert project.canvas is None
    assert project.canvas_ratio == "16:9"
    assert "canvas" not in timeline.to_dict(project)


V2_FILE = {
    "version": 2,
    "revision": 4,
    "tracks": [
        {
            "id": "t",
            "kind": "video",
            "magnetic": True,
            "locked": True,
            "muted": True,
            "clips": [
                {
                    "id": "c",
                    "source": "a.mp4",
                    "in": 1,
                    "out": 9,
                    "crop": {"x": 0.25, "y": 0, "w": 0.5, "h": 1},
                    "label": "Song 1",
                    "label_color": "#ff0000",
                    "included": False,
                    "export_name": "one",
                }
            ],
        }
    ],
}


def test_a_version_2_file_migrates_on_load_and_keeps_its_fields():
    assert timeline.FORMAT_VERSION == 3
    migrated = timeline.migrate(V2_FILE)
    assert migrated["version"] == 3 and V2_FILE["version"] == 2  # a new dict
    project = timeline.from_dict(V2_FILE)
    track, clip = project.tracks[0], project.tracks[0].clips[0]
    assert project.canvas is None and (track.locked, track.muted, track.hidden) == (True, True, False)
    assert clip.crop == {"x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0}
    assert (clip.label, clip.label_color, clip.included, clip.export_name) == ("Song 1", "#ff0000", False, "one")
    again = timeline.to_dict(project)
    assert again["version"] == 3 and "canvas" not in again
    assert again["tracks"][0]["clips"][0]["crop"] == clip.crop


def test_a_version_3_file_round_trips_with_its_canvas():
    data = {**timeline.migrate(V2_FILE), "canvas": "1:1"}
    project = timeline.from_dict(data)
    assert project.canvas == "1:1"
    assert timeline.from_dict(json.loads(json.dumps(timeline.to_dict(project)))) == project
    assert timeline.to_dict(project)["canvas"] == "1:1"


def test_a_version_1_file_still_loads_and_a_stray_v2_canvas_is_not_a_choice():
    assert timeline.from_dict({"tracks": []}).canvas is None  # no version: version 1
    assert timeline.from_dict({"version": 2, "canvas": "9:16", "tracks": []}).canvas is None


def test_the_first_edit_of_a_version_2_file_writes_version_3(tmp_path):
    w = World("local", tmp_path)
    timeline_service.create_project(w.store, NAME)
    timeline_service.add_clip(w.store, w.media, NAME, asset=w.ids["a"], in_point=0, out_point=30)
    path = tmp_path / f"{NAME}.split-video-timeline.json"
    old = json.loads(path.read_text())
    path.write_text(json.dumps({**old, "version": 2}))
    set_canvas(w, "9:16")
    saved = json.loads(path.read_text())
    assert saved["version"] == 3 and saved["canvas"] == "9:16"


def test_an_unknown_canvas_in_a_file_is_refused():
    with pytest.raises(ValueError, match="unknown canvas"):
        timeline.from_dict({"version": 3, "canvas": "4:3", "tracks": []})


def test_the_canvas_round_trips_and_survives_other_edits(world):
    seeded(world)
    assert view(world)["canvas"] == {"ratio": "16:9", "preset": "youtube-1080p", "needs_reframe": []}
    result = set_canvas(world, "9:16")
    assert result["results"][0]["summary"] == "set canvas 9:16"
    timeline_service.add_clip(world.store, world.media, NAME, asset=world.ids["b"], in_point=0, out_point=10)
    assert world.store.load_timeline(NAME).canvas == "9:16"
    assert view(world)["canvas"]["preset"] == "vertical-1080"
    if world.kind == "local":
        saved = json.loads((world.media.root / f"{NAME}.split-video-timeline.json").read_text())
        assert saved["canvas"] == "9:16"
        assert saved["version"] == 3


def test_switching_keeps_every_clip_and_flags_those_of_another_shape(world):
    seeded(world)  # 640x360 sources: 16:9
    clips = [c["id"] for c in view(world)["tracks"][0]["clips"]]
    assert set_canvas(world, "16:9")["clips"] == 1
    assert view(world)["canvas"]["needs_reframe"] == []
    set_canvas(world, "1:1")
    after = view(world)
    assert [c["id"] for c in after["tracks"][0]["clips"]] == clips
    assert after["canvas"]["needs_reframe"] == clips


def test_a_clip_cropped_to_the_canvas_shape_is_not_flagged(world):
    seeded(world)
    clip = view(world)["tracks"][0]["clips"][0]["id"]
    # 640x360 cropped to a 9:16 window: w = 0.31640625 of the width, full height
    edit_ops.apply_ops(
        world.media,
        world.store,
        {
            "schema_version": 1,
            "project": NAME,
            "ops": [{"op": "set_clip_properties", "clip": clip, "crop": {"x": 0.34, "y": 0, "w": 0.31640625, "h": 1}}],
        },
    )
    set_canvas(world, "9:16")
    assert view(world)["canvas"]["needs_reframe"] == []


def test_an_unknown_ratio_is_refused_and_a_stale_revision_conflicts(world):
    seeded(world)
    with pytest.raises(OpsInvalidError):
        set_canvas(world, "4:3")
    with pytest.raises(RevisionConflictError):
        set_canvas(world, "9:16", base_revision=0)
    assert world.store.load_timeline(NAME).canvas is None


def test_renders_and_previews_without_a_preset_use_the_canvas():
    assert presets.canvas_preset(None, None, None, "vertical-1080") == "vertical-1080"
    assert presets.canvas_preset(None, None, None, None) is None
    assert presets.canvas_preset("draft-720", None, None, "vertical-1080") == "draft-720"
    assert presets.canvas_preset(None, 640, 360, "vertical-1080") is None
