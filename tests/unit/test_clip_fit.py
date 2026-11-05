"""SPL-122: per-clip canvas fit and main-track placement: the format change, the rules and the filter graph."""

import pytest

from split_video import edit_ops, timeline
from split_video import timeline_ops as ops
from split_video.ffmpeg import MediaInfo
from split_video.render import build_plan
from split_video.timeline import TimelineError, empty_project

INFO = {"a.mp4": MediaInfo(duration=10.0, width=640, height=360, has_audio=False)}


def project(**props):
    p = empty_project()
    p, clip = ops.add_clip(p, p.tracks[0].id, "a.mp4", 10.0, out_point=3.0)
    return (ops.set_clip_properties(p, clip.id, **props) if props else p), clip.id


def graph(p, fit="letterbox"):
    return build_plan(p, INFO, 480, 270, fit=fit, audio=False).graph


def test_a_version_3_file_migrates_with_neutral_main_track_placement_and_no_fit():
    clip = {"id": "c", "source": "a.mp4", "in": 0, "out": 3, "x": 0.2, "scale": 0.5, "opacity": 0.4}
    overlay = {**clip, "id": "o"}
    data = {
        "version": 3,
        "tracks": [
            {"id": "m", "kind": "video", "magnetic": True, "clips": [clip]},
            {"id": "t", "kind": "video", "magnetic": False, "clips": [overlay]},
        ],
    }
    assert timeline.FORMAT_VERSION == 4
    migrated = timeline.from_dict(data)
    main, over = migrated.tracks[0].clips[0], migrated.tracks[1].clips[0]
    assert (main.x, main.scale, main.opacity, main.fit) == (0.5, 1.0, 1.0, "default")
    assert (over.x, over.scale, over.opacity) == (0.2, 0.5, 0.4)  # an overlay keeps what it showed
    assert (
        timeline.to_dict(migrated)["version"] == 4 and "fit" not in timeline.to_dict(migrated)["tracks"][0]["clips"][0]
    )


def test_a_fit_round_trips_and_is_written_only_when_set():
    p, cid = project(fit="crop")
    assert timeline.from_dict(timeline.to_dict(p)) == p
    assert timeline.to_dict(p)["tracks"][0]["clips"][0]["fit"] == "crop"
    p2, _ = project()
    assert "fit" not in timeline.to_dict(p2)["tracks"][0]["clips"][0]


def test_a_fit_must_be_known_and_belong_to_a_picture():
    p, cid = project()
    with pytest.raises(TimelineError, match="fit must be one of"):
        ops.set_clip_properties(p, cid, fit="stretch")
    p, track = ops.add_track(p, "video")
    p, text = ops.add_text_clip(p, track.id, "hi", 1.0)
    with pytest.raises(TimelineError, match="picture clip"):
        ops.set_clip_properties(p, text.id, fit="crop")


def test_the_edit_op_schema_offers_fit():
    assert "fit" in {f.name for f in edit_ops.OPS["set_clip_properties"].fields}


def test_defaults_keep_todays_graph_and_a_clip_fit_overrides_the_export_fit():
    plain, _ = project()
    assert "pad=480:270" in graph(plain) and "overlay=x=" not in graph(plain)
    own, _ = project(fit="crop")
    assert "force_original_aspect_ratio=increase" in graph(own, "letterbox")
    assert "force_original_aspect_ratio=increase" not in graph(plain, "letterbox")
    assert "force_original_aspect_ratio=increase" in graph(plain, "crop")
    letter, _ = project(fit="letterbox")
    assert "force_original_aspect_ratio=increase" not in graph(letter, "crop")


def test_main_track_scale_position_and_opacity_are_in_the_graph():
    p, _ = project(scale=0.5, x=0.25, y=0.4, opacity=0.5)
    g = graph(p)
    assert "colorchannelmixer=aa=0.5" in g and "overlay=x=120-w/2:y=108-h/2" in g and "trunc(iw*0.5/2)" in g
