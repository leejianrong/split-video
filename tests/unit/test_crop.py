"""Clip crop (SPL-51): validation, persistence, the aspect helper, the filter graph, plans and ops."""

import json

import pytest
from jsonschema import Draft202012Validator
from test_edit_plan import World

from split_video import edit_ops, edit_plan, timeline, timeline_ops, timeline_service
from split_video.edit_ops import OpsInvalidError
from split_video.ffmpeg import MediaInfo
from split_video.render import build_plan, check_size, parse_size
from split_video.timeline import TimelineError, empty_project

INFO = MediaInfo(10, 1920, 1080, True)
RIGHT_HALF = {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}


def _project():
    p = empty_project()
    p, clip = timeline_ops.add_clip(p, p.tracks[0].id, "a.mp4", 10.0)
    return p, clip.id


@pytest.fixture(params=["memory", "local"])
def world(request, tmp_path):
    return World(request.param, tmp_path)


# --- validation ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "box",
    [
        (-0.1, 0, 0.5, 0.5),
        (0, 1.1, 0.5, 0.5),
        (0, 0, 0.01, 0.5),  # below the minimum
        (0, 0, 0.5, 0.0),
        (0.6, 0, 0.5, 1.0),  # x + w > 1
        (0, 0.6, 1.0, 0.5),  # y + h > 1
        (0, 0, 1.5, 1.0),
        (float("nan"), 0, 0.5, 0.5),
        (True, 0, 0.5, 0.5),
        ("0", 0, 0.5, 0.5),
    ],
)
def test_bad_crops_have_one_stable_error(box):
    with pytest.raises(timeline_ops.RuleViolation) as caught:
        timeline_ops.check_crop(*box)
    assert caught.value.reason == "crop_invalid" and str(caught.value) == timeline_ops.CROP_ERROR


def test_crop_accepts_the_edges_and_float_rounding():
    assert timeline_ops.check_crop(0, 0, 1, 1) == {"x": 0, "y": 0, "w": 1, "h": 1}
    assert timeline_ops.check_crop(0.02, 0, 0.98, 1)["w"] == 0.98
    got = timeline_ops.check_crop(0.7, 0.0, 0.3 + 5e-7, 1.0)  # an overshoot of rounding size is trimmed
    assert got["x"] + got["w"] <= 1.0
    assert timeline_ops.check_crop(0, 0, timeline_ops.CROP_MIN, timeline_ops.CROP_MIN)["w"] == timeline_ops.CROP_MIN


def test_set_clip_properties_sets_and_clears_a_crop():
    p, cid = _project()
    p = timeline_ops.set_clip_properties(p, cid, crop=RIGHT_HALF, gain=0.5)
    clip = timeline_ops.find_clip_in(p, cid)
    assert clip.crop == RIGHT_HALF and clip.gain == 0.5 and clip.cropped
    p = timeline_ops.set_clip_properties(p, cid, crop=None)
    clip = timeline_ops.find_clip_in(p, cid)
    assert clip.crop is None and not clip.cropped


@pytest.mark.parametrize(
    "bad", [{"x": 0.5, "y": 0, "w": 0.6, "h": 1}, {"x": 0, "y": 0, "w": 1}, 3, {"x": 0, "y": 0, "w": 1, "h": 1, "z": 2}]
)
def test_set_clip_properties_rejects_a_bad_crop_and_leaves_the_project(bad):
    p, cid = _project()
    with pytest.raises(timeline_ops.RuleViolation) as caught:
        timeline_ops.set_clip_properties(p, cid, crop=bad)
    assert caught.value.reason == "crop_invalid"


def test_text_and_audio_clips_cannot_be_cropped():
    p = empty_project()
    p, overlay = timeline_ops.add_track(p, "video")
    p, text = timeline_ops.add_text_clip(p, overlay.id, "hi", 2.0)
    with pytest.raises(timeline_ops.RuleViolation) as caught:
        timeline_ops.set_clip_properties(p, text.id, crop=RIGHT_HALF)
    assert caught.value.reason == "crop_not_applicable"
    p, audio = timeline_ops.add_track(p, "audio")
    p, clip = timeline_ops.add_clip(p, audio.id, "song.mp3", 5.0)
    with pytest.raises(timeline_ops.RuleViolation) as caught:
        timeline_ops.set_clip_properties(p, clip.id, crop=RIGHT_HALF)
    assert caught.value.reason == "crop_not_applicable"


# --- persistence: the format stays version 2 ------------------------------------------------------------------------


def test_format_version_stays_two_and_a_file_without_crop_loads_unchanged():
    assert timeline.FORMAT_VERSION == 2
    old = {
        "version": 2,
        "tracks": [{"id": "t", "kind": "video", "clips": [{"id": "c", "source": "a.mp4", "in": 0, "out": 4}]}],
    }
    project = timeline.from_dict(old)
    clip = project.tracks[0].clips[0]
    assert (clip.crop_x, clip.crop_y, clip.crop_w, clip.crop_h) == (0.0, 0.0, 1.0, 1.0) and clip.crop is None
    assert "crop" not in timeline.to_dict(project)["tracks"][0]["clips"][0]  # omitted when it is the default


def test_a_crop_round_trips_and_is_omitted_when_default():
    p, cid = _project()
    p = timeline_ops.set_clip_properties(p, cid, crop=RIGHT_HALF)
    data = json.loads(json.dumps(timeline.to_dict(p)))
    assert data["version"] == 2 and data["tracks"][0]["clips"][0]["crop"] == RIGHT_HALF
    assert timeline.from_dict(data) == p
    cleared = timeline_ops.set_clip_properties(p, cid, crop=None)
    assert "crop" not in timeline.to_dict(cleared)["tracks"][0]["clips"][0]


def test_crop_is_saved_by_the_service(world):
    timeline_service.create_project(world.store, "m")
    timeline_service.add_clip(world.store, world.media, "m", asset=world.ids["a"], in_point=0, out_point=30)
    cid = world.store.load_timeline("m").tracks[0].clips[0].id
    timeline_service.set_clip_properties(world.store, "m", cid, media=world.media, crop=RIGHT_HALF)
    saved = world.store.load_timeline("m").tracks[0].clips[0]
    assert saved.crop == RIGHT_HALF
    shown = timeline_service.describe(world.media, "m", world.store.load_timeline("m"))
    assert shown["tracks"][0]["clips"][0]["crop"] == RIGHT_HALF
    with pytest.raises(TimelineError):
        timeline_service.set_clip_properties(
            world.store, "m", cid, media=world.media, crop={"x": 1, "y": 0, "w": 1, "h": 1}
        )
    assert world.store.load_timeline("m").tracks[0].clips[0].crop == RIGHT_HALF


def test_set_clip_crop_by_aspect_uses_the_source_size(world):
    timeline_service.create_project(world.store, "m")
    timeline_service.add_clip(world.store, world.media, "m", asset=world.ids["a"], in_point=0, out_point=30)  # 640x360
    cid = world.store.load_timeline("m").tracks[0].clips[0].id
    p = timeline_service.set_clip_crop(world.store, "m", cid, media=world.media, aspect="9:16", center_x=0.0)
    crop = p.tracks[0].clips[0].crop
    assert crop["x"] == 0.0 and crop["h"] == 1.0 and crop["w"] == pytest.approx(0.31640625, abs=1e-6)
    p = timeline_service.set_clip_crop(world.store, "m", cid, media=world.media, clear=True)
    assert p.tracks[0].clips[0].crop is None
    with pytest.raises(TimelineError, match="exactly one"):
        timeline_service.set_clip_crop(world.store, "m", cid, media=world.media)


# --- the helper -----------------------------------------------------------------------------------------------------


def test_crop_for_aspect_centres_a_vertical_slice_of_a_wide_frame():
    crop = timeline_ops.crop_for_aspect(1920, 1080, 9, 16)
    assert crop["h"] == 1.0 and crop["y"] == 0.0
    assert crop["w"] == pytest.approx(0.31640625, abs=1e-6) and crop["x"] == pytest.approx(0.341797, abs=1e-6)
    assert crop["w"] * 1920 / (crop["h"] * 1080) == pytest.approx(9 / 16, rel=1e-5)


def test_crop_for_aspect_off_centre_is_clamped_inside_the_frame():
    left = timeline_ops.crop_for_aspect(1920, 1080, 9, 16, center_x=0.0)
    assert left["x"] == 0.0
    right = timeline_ops.crop_for_aspect(1920, 1080, 9, 16, center_x=1.0)
    assert right["x"] + right["w"] <= 1.0 and right["x"] == pytest.approx(1 - right["w"], abs=1e-6)
    mid = timeline_ops.crop_for_aspect(1920, 1080, 9, 16, center_x=0.3)
    assert mid["x"] + mid["w"] / 2 == pytest.approx(0.3, abs=1e-6)


def test_crop_for_aspect_zoom_shrinks_and_centres_on_the_point():
    crop = timeline_ops.crop_for_aspect(1920, 1080, 9, 16, center_x=0.5, center_y=0.25, zoom=2.0)
    assert crop["w"] == pytest.approx(0.158203, abs=1e-6) and crop["h"] == pytest.approx(0.5)
    assert crop["y"] + crop["h"] / 2 == pytest.approx(0.25, abs=1e-6)
    assert crop["w"] * 1920 / (crop["h"] * 1080) == pytest.approx(9 / 16, rel=1e-4)


def test_crop_for_aspect_taller_source_and_same_aspect():
    tall = timeline_ops.crop_for_aspect(1080, 1920, 16, 9)  # a wide slice of a phone video: full width
    assert tall["w"] == 1.0 and tall["h"] == pytest.approx(0.31640625, abs=1e-6)
    assert timeline_ops.crop_for_aspect(1920, 1080, 16, 9) == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


@pytest.mark.parametrize(
    "kwargs",
    [{"zoom": 0.5}, {"zoom": 100}, {"center_x": 1.5}, {"aspect_w": 0}, {"source_w": 0}, {"zoom": float("nan")}],
)
def test_crop_for_aspect_rejects_bad_input(kwargs):
    args = {"source_w": 1920, "source_h": 1080, "aspect_w": 9, "aspect_h": 16, **kwargs}
    with pytest.raises(TimelineError):
        timeline_ops.crop_for_aspect(**args)


def test_the_helper_result_is_always_a_valid_crop():
    for cx in (0, 0.1, 0.5, 0.99, 1):
        for zoom in (1, 1.3, 4):
            crop = timeline_ops.crop_for_aspect(1280, 720, 9, 16, cx, 0.5, zoom)
            assert timeline_ops.check_crop(**crop) == crop


def test_parse_aspect():
    assert timeline_ops.parse_aspect("9:16") == (9.0, 16.0)
    for bad in ("9", "9:0", "a:b", "9:16:1", ""):
        with pytest.raises(TimelineError):
            timeline_ops.parse_aspect(bad)


# --- the filter graph -----------------------------------------------------------------------------------------------


def test_main_clip_is_cropped_before_it_is_scaled():
    p, cid = _project()
    p = timeline_ops.set_clip_properties(p, cid, crop=RIGHT_HALF, brightness=0.1)
    graph = build_plan(p, {"a.mp4": INFO}, 540, 960).graph
    # right half of 1920x1080 = 960x1080 at x=960, then fitted to 540x960 (an exact 9:16 would fill; this is 8:9)
    assert ",crop=960:1080:960:0," in graph
    assert graph.index("crop=") < graph.index("scale=540:960") < graph.index("eq=") < graph.index("pad=")


def test_matching_aspect_fills_the_frame_with_one_scale():
    crop = timeline_ops.crop_for_aspect(1920, 1080, 9, 16)
    p, cid = _project()
    p = timeline_ops.set_clip_properties(p, cid, crop=crop)
    graph = build_plan(p, {"a.mp4": INFO}, 1080, 1920).graph
    assert "crop=608:1080:656:0,scale=1080:1920," in graph  # no force_original_aspect_ratio: no bars


def test_uncropped_clip_graph_is_unchanged():
    p, _ = _project()
    graph = build_plan(p, {"a.mp4": INFO}, 1280, 720).graph
    assert "crop" not in graph and "scale=1280:720:force_original_aspect_ratio=decrease" in graph


def test_overlay_clip_is_cropped_before_it_is_scaled():
    p, _ = _project()
    p, track = timeline_ops.add_track(p, "video")
    p, clip = timeline_ops.add_clip(p, track.id, "pip.mp4", 3.0, start=1.0)
    p = timeline_ops.set_clip_properties(p, clip.id, crop={"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}, scale=0.5)
    graph = build_plan(p, {"a.mp4": INFO, "pip.mp4": INFO}, 1280, 720).graph
    assert "fps=30,crop=960:540:480:270,scale=640:-2" in graph


def test_crop_stays_inside_the_frame_on_odd_sources():
    p, cid = _project()
    p = timeline_ops.set_clip_properties(p, cid, crop={"x": 0.3, "y": 0.3, "w": 0.7, "h": 0.7})
    graph = build_plan(p, {"a.mp4": MediaInfo(10, 641, 361, True)}, 640, 360).graph
    crop = graph.split("crop=")[1].split(",")[0].split(":")
    cw, ch, cx, cy = (int(v) for v in crop)
    assert cx + cw <= 641 and cy + ch <= 361 and cw % 2 == 0 and ch % 2 == 0


def test_output_size_is_checked():
    assert check_size(None, None) is None and check_size(1080, 1920) == (1080, 1920)
    assert parse_size("1080x1920") == (1080, 1920) and parse_size(None) == (None, None)
    for bad in ((1080, None), (1081, 1920), (32, 1920), (1080, 8192), (1080.0, 1920), (True, 1920)):
        with pytest.raises(TimelineError):
            check_size(*bad)
    for bad in ("1080", "axb", "1080x1920x2", "-1x4"):
        with pytest.raises(TimelineError):
            parse_size(bad)


def test_plan_render_rejects_a_bad_size_before_anything_starts(world):
    timeline_service.create_project(world.store, "m")
    timeline_service.add_clip(world.store, world.media, "m", asset=world.ids["a"], in_point=0, out_point=3)
    with pytest.raises(TimelineError):
        timeline_service.plan_render(world.store, world.media, "m", None, False, 1081, 1920)
    assert timeline_service.plan_render(world.store, world.media, "m", None, False, 1080, 1920)[1] == "m-render.mp4"


# --- edit plans and ops ---------------------------------------------------------------------------------------------


def _plan(world, clip):
    return {
        "schema_version": 1,
        "project": "reel",
        "tracks": [{"kind": "video", "clips": [{"asset": world.ids["a"], "in": 0, "out": 10, **clip}]}],
    }


def test_plan_with_crop_is_valid_and_builds_the_clip(world):
    project = edit_plan.build_timeline(world.media, _plan(world, {"crop": RIGHT_HALF}))
    assert project.tracks[0].clips[0].crop == RIGHT_HALF
    assert edit_plan.build_timeline(world.media, _plan(world, {})).tracks[0].clips[0].crop is None


@pytest.mark.parametrize(
    "crop",
    [{"x": 0.5, "y": 0, "w": 0.6, "h": 1}, {"x": 0, "y": 0, "w": 0.001, "h": 1}, {"x": -1, "y": 0, "w": 1, "h": 1}],
)
def test_plan_rejects_a_bad_crop_with_a_stable_code(world, crop):
    report = edit_plan.validate_plan(world.media, _plan(world, {"crop": crop}))
    assert [(e.code, e.path) for e in report.errors] == [("plan.clip.crop_invalid", "/tracks/0/clips/0/crop")]


def test_plan_crop_shape_errors_use_the_field_codes(world):
    report = edit_plan.validate_plan(world.media, _plan(world, {"crop": {"x": 0, "y": 0, "w": 1}}))
    assert [e.code for e in report.errors] == ["plan.field.required"]
    report = edit_plan.validate_plan(world.media, _plan(world, {"crop": {"x": 0, "y": 0, "w": 1, "h": 1, "z": 1}}))
    assert [e.code for e in report.errors] == ["plan.field.unknown"]
    report = edit_plan.validate_plan(world.media, _plan(world, {"crop": "half"}))
    assert [e.code for e in report.errors] == ["plan.field.type"]


def test_plan_crop_is_not_applicable_to_text_or_audio_clips(world):
    doc = _plan(world, {})
    doc["tracks"].append(
        {"kind": "video", "magnetic": False, "clips": [{"kind": "text", "text": "hi", "out": 2, "crop": RIGHT_HALF}]}
    )
    doc["tracks"].append(
        {"kind": "audio", "clips": [{"asset": world.ids["song"], "in": 0, "out": 5, "crop": RIGHT_HALF}]}
    )
    report = edit_plan.validate_plan(world.media, doc)
    assert [(e.code, e.path) for e in report.errors] == [
        ("plan.clip.field_not_applicable", "/tracks/1/clips/0/crop"),
        ("plan.clip.field_not_applicable", "/tracks/2/clips/0/crop"),
    ]


def test_published_schemas_describe_crop_and_the_plan_example_validates():
    schema = edit_plan.schema()
    crop = schema["$defs"]["clip"]["properties"]["crop"]
    assert crop["type"] == "object" and crop["additionalProperties"] is False
    assert crop["required"] == ["x", "y", "w", "h"] and set(crop["properties"]) == {"x", "y", "w", "h"}
    assert schema["properties"]["schema_version"]["const"] == 1  # additive: still version 1
    validator = Draft202012Validator(schema)
    ok = {
        "schema_version": 1,
        "project": "p",
        "tracks": [{"kind": "video", "clips": [{"asset": "a_0123456789abcdef", "crop": RIGHT_HALF}]}],
    }
    assert list(validator.iter_errors(ok)) == []
    bad = json.loads(json.dumps(ok))
    del bad["tracks"][0]["clips"][0]["crop"]["h"]
    assert list(validator.iter_errors(bad))
    ops = edit_ops.schema()["$defs"]["set_clip_properties"]["properties"]["crop"]
    assert ops["type"] == ["object", "null"] and "required" in ops


def _seed(world):
    timeline_service.create_project(world.store, "reel")
    timeline_service.add_clip(world.store, world.media, "reel", asset=world.ids["a"], in_point=0, out_point=30)
    return world.store.load_timeline("reel").tracks[0].clips[0].id


def _ops(*ops):
    return {"schema_version": 1, "project": "reel", "ops": list(ops)}


def test_ops_set_and_clear_a_crop(world):
    cid = _seed(world)
    edit_ops.apply_ops(world.media, world.store, _ops({"op": "set_clip_properties", "clip": cid, "crop": RIGHT_HALF}))
    assert world.store.load_timeline("reel").tracks[0].clips[0].crop == RIGHT_HALF
    edit_ops.apply_ops(world.media, world.store, _ops({"op": "set_clip_properties", "clip": cid, "crop": None}))
    assert world.store.load_timeline("reel").tracks[0].clips[0].crop is None


def test_ops_reject_a_bad_crop_and_write_nothing(world):
    cid = _seed(world)
    bad = {"x": 0.5, "y": 0, "w": 0.6, "h": 1}
    with pytest.raises(OpsInvalidError) as caught:
        edit_ops.apply_ops(world.media, world.store, _ops({"op": "set_clip_properties", "clip": cid, "crop": bad}))
    assert [(e.code, e.path) for e in caught.value.report.errors] == [("plan.clip.crop_invalid", "/ops/0/crop")]
    assert world.store.load_timeline("reel").tracks[0].clips[0].crop is None


def test_ops_crop_on_a_title_is_not_applicable(world):
    cid = _seed(world)
    ops = _ops(
        {"op": "add_track", "id": "ov", "kind": "video"},
        {"op": "add_text_clip", "id": "tx", "track": "ov", "text": "hi", "duration": 2},
        {"op": "set_clip_properties", "clip": "tx", "crop": RIGHT_HALF},
    )
    with pytest.raises(OpsInvalidError) as caught:
        edit_ops.apply_ops(world.media, world.store, ops)
    assert [e.code for e in caught.value.report.errors] == ["plan.clip.field_not_applicable"]
    assert cid
