"""Export presets (SPL-52): the table, how a request resolves, and what reaches the ffmpeg graph and arguments."""

import pytest

from split_video import presets, timeline_ops
from split_video import render as render_mod
from split_video.ffmpeg import MediaInfo
from split_video.presets import PresetError, resolve_export
from split_video.render import _encode_args, build_plan
from split_video.timeline import TimelineError, empty_project

INFO = MediaInfo(10, 640, 360, True)


def _project(*, overlay=False, crop=None):
    p = empty_project()
    p, clip = timeline_ops.add_clip(p, p.tracks[0].id, "a.mp4", 5.0)
    p, _ = timeline_ops.add_clip(p, p.tracks[0].id, "b.mp4", 5.0)
    if crop:
        p = timeline_ops.set_clip_properties(p, clip.id, crop=crop)
    if overlay:
        p, track = timeline_ops.add_track(p, "video")
        p, o = timeline_ops.add_clip(p, track.id, "c.mp4", 2.0, start=1.0)
        p = timeline_ops.set_clip_properties(p, o.id, scale=0.5)
    return p


def _infos(p):
    return {c.source: INFO for t in p.tracks for c in t.clips}


# --- the table --------------------------------------------------------------------------------------------------------


def test_the_required_presets_exist_with_the_agreed_sizes():
    got = {n: (p.width, p.height, p.fps) for n, p in presets.PRESETS.items()}
    assert got["youtube-1080p"] == (1920, 1080, 30)
    assert got["vertical-1080"] == (1080, 1920, 30)
    assert got["square-1080"][:2] == (1080, 1080)
    assert got["draft-720"][:2] == (1280, 720) and got["draft-720"][2] in (24, 30)


@pytest.mark.parametrize("preset", presets.PRESETS.values(), ids=lambda p: p.name)
def test_every_preset_is_sane(preset):
    assert preset.name == preset.name.lower() and " " not in preset.name
    assert preset.width % 2 == 0 and preset.height % 2 == 0  # yuv420p
    assert render_mod.MIN_SIZE <= preset.width <= render_mod.MAX_SIZE
    assert render_mod.MIN_SIZE <= preset.height <= render_mod.MAX_SIZE
    assert render_mod.check_size(preset.width, preset.height) == (preset.width, preset.height)
    assert 1 <= preset.fps <= 60
    assert (preset.video_codec, preset.pix_fmt, preset.audio_codec) == ("libx264", "yuv420p", "aac")
    assert 0 <= preset.crf <= 51 and preset.audio_sample_rate == 48000
    assert (preset.container, preset.faststart) == ("mp4", True)
    assert preset.label and preset.description


def test_draft_is_a_faster_and_lighter_setting_than_the_delivery_presets():
    draft, youtube = presets.get("draft-720"), presets.get("youtube-1080p")
    assert draft.x264_preset == "ultrafast" and draft.crf > youtube.crf


def test_listing_has_every_field_of_every_preset_sorted_by_name():
    listing = presets.listing()
    assert [p["name"] for p in listing["presets"]] == sorted(presets.PRESETS)
    assert set(listing["presets"][0]) == set(presets.ExportPreset.__dataclass_fields__)
    assert listing["fits"] == ["letterbox", "crop"] and listing["default_fit"] == "letterbox"


# --- resolving a request ----------------------------------------------------------------------------------------------


def test_without_a_preset_nothing_changes():
    s = resolve_export(None, None, None)
    assert (s.preset, s.size, s.fps, s.fit) == (None, None, render_mod.FPS, "letterbox")


def test_precedence_is_explicit_size_then_preset_then_the_first_clip():
    assert resolve_export("vertical-1080", None, None).size == (1080, 1920)
    assert resolve_export("vertical-1080", 540, 960).size == (540, 960)  # explicit wins, both allowed
    assert resolve_export(None, 540, 960).size == (540, 960)
    s = resolve_export("draft-720", 640, 360)
    assert s.size == (640, 360) and s.fps == 24 and s.preset.name == "draft-720"  # fps/encoder still the preset's


def test_a_half_given_explicit_size_is_still_an_error_with_a_preset():
    with pytest.raises(TimelineError, match="both width and height"):
        resolve_export("vertical-1080", 540, None)


def test_an_unknown_preset_lists_the_valid_names():
    with pytest.raises(PresetError) as caught:
        resolve_export("tiktok", None, None)
    assert isinstance(caught.value, TimelineError)
    assert str(caught.value) == (
        "unknown preset 'tiktok'; valid presets: draft-720, square-1080, vertical-1080, youtube-1080p"
    )


def test_an_unknown_fit_lists_the_valid_fits():
    with pytest.raises(PresetError, match=r"unknown fit 'stretch'; valid fits: letterbox, crop"):
        resolve_export(None, None, None, "stretch")


# --- encoder arguments ------------------------------------------------------------------------------------------------


def test_no_preset_keeps_todays_encoder_arguments():
    assert _encode_args(None) == ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart"]


def test_preset_arguments_carry_codec_quality_audio_and_faststart():
    args = _encode_args(presets.get("draft-720"))
    pairs = dict(zip(args[::2], args[1::2]))
    assert pairs["-c:v"] == "libx264" and pairs["-preset"] == "ultrafast" and pairs["-crf"] == "30"
    assert pairs["-pix_fmt"] == "yuv420p" and pairs["-c:a"] == "aac" and pairs["-b:a"] == "96k"
    assert pairs["-ar"] == "48000" and pairs["-movflags"] == "+faststart"


# --- the graph --------------------------------------------------------------------------------------------------------


def test_the_frame_rate_is_a_parameter_of_the_graph():
    p = _project(overlay=True)
    plan = build_plan(p, _infos(p), 1280, 720, fps=24)
    assert plan.fps == 24 and "fps=24" in plan.graph and "r=24:" in plan.graph
    assert "fps=30" not in plan.graph and "r=30" not in plan.graph
    assert "fps=30" in build_plan(p, _infos(p), 1280, 720).graph  # default unchanged


def test_crossfade_after_a_cut_retimes_to_the_chosen_frame_rate():
    p = _project()
    p, _ = timeline_ops.add_clip(p, p.tracks[0].id, "c.mp4", 3.0)
    p = timeline_ops.set_transition(p, p.tracks[0].clips[2].id, "crossfade", 1)  # clip 1 -> 2 is a cut, 2 -> 3 fades
    graph = build_plan(p, _infos(p), 640, 360, fps=24).graph
    assert "settb=1/24" in graph and "settb=1/30" not in graph


def test_letterbox_is_todays_graph_and_crop_covers_the_frame():
    p = _project()
    letterbox = build_plan(p, _infos(p), 1080, 1920).graph
    assert build_plan(p, _infos(p), 1080, 1920, fit="letterbox").graph == letterbox
    assert "force_original_aspect_ratio=decrease" in letterbox and "increase" not in letterbox
    cover = build_plan(p, _infos(p), 1080, 1920, fit="crop").graph
    assert "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" in cover
    assert "decrease" not in cover


def test_a_clip_with_its_own_crop_keeps_crop_then_fit_under_either_fit():
    crop = {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}
    p = _project(crop=crop)
    graph = build_plan(p, _infos(p), 1080, 1920, fit="crop").graph
    first = graph.split(";")[0]
    assert "crop=320:360:320:0" in first and "force_original_aspect_ratio=decrease" in first
    assert "increase" in graph.split(";")[2]  # the second, uncropped clip covers


def test_overlays_cover_a_frame_shaped_box_under_crop_fit():
    p = _project(overlay=True)
    letterbox = build_plan(p, _infos(p), 1080, 1920).graph
    assert "scale=540:-2" in letterbox
    cover = build_plan(p, _infos(p), 1080, 1920, fit="crop").graph
    assert "scale=540:960:force_original_aspect_ratio=increase,crop=540:960" in cover and "scale=540:-2" not in cover
