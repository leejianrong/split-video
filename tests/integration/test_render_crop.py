"""Rendering cropped clips for real (SPL-51): decode frames and look at the pixels.

The source is 640x360: the left half red with a white marker square, the right half blue. Cropping
to the right half (or to a 9:16 slice inside it) must show only blue; any red would mean the crop did
not apply or leaked.
"""

import json
import subprocess

import numpy as np
import pytest
from typer.testing import CliRunner

from split_video import edit_ops, edit_plan, timeline_ops, timeline_service
from split_video.cli import app
from split_video.ffmpeg import probe_duration, probe_media
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.render import render
from split_video.timeline import TimelineError, empty_project

RIGHT_HALF = {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}


def _ffmpeg(*args):
    result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("crop")
    enc = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=red:size=320x360:rate=25:duration=2",
        "-f", "lavfi", "-i", "color=c=blue:size=320x360:rate=25:duration=2", *tone,
        "-filter_complex", "[0][1]hstack,drawbox=x=140:y=160:w=40:h=40:color=white:t=fill[v]",
        "-map", "[v]", "-map", "2:a", *enc, str(root / "split.mp4"),
    )  # fmt: skip
    _ffmpeg("-f", "lavfi", "-i", "color=c=0x00ff00:size=640x360:rate=25:duration=2", *enc, str(root / "green.mp4"))
    return root


def _frame(path, t):
    """The frame at `t` seconds as an (h, w, 3) uint8 RGB array."""
    info = probe_media(path)
    out = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(out, dtype=np.uint8).reshape(info.height, info.width, 3)


def _share(frame, color, tol=40):
    """The fraction of pixels within `tol` of `color` on every channel."""
    return float((np.abs(frame.astype(int) - np.array(color)) <= tol).all(axis=2).mean())


def _one_clip(source="split.mp4", crop=None, duration=2.0):
    p = empty_project()
    p, clip = timeline_ops.add_clip(p, p.tracks[0].id, source, duration)
    if crop:
        p = timeline_ops.set_clip_properties(p, clip.id, crop=crop)
    return p


def _render(root, project, name="out.mp4", **size):
    render(project, LocalMediaStore(root), name, **size)
    return root / name


def test_a_9x16_crop_fills_a_vertical_frame_with_only_the_cropped_region(root):
    crop = timeline_ops.crop_for_aspect(640, 360, 9, 16, center_x=0.75)  # inside the blue half
    out = _render(root, _one_clip(crop=crop), width=360, height=640)
    info = probe_media(out)
    assert (info.width, info.height) == (360, 640)
    frame = _frame(out, 1.0)
    assert _share(frame, (0, 0, 255)) > 0.999  # every pixel blue: no red leaked, no black bars
    assert _share(frame, (0, 0, 0), tol=30) == 0.0


def test_the_vertical_size_is_exact_for_the_full_1080x1920_frame(root):
    crop = timeline_ops.crop_for_aspect(640, 360, 9, 16, center_x=0.75)
    out = _render(root, _one_clip(crop=crop, duration=1.0), "full.mp4", width=1080, height=1920)
    assert (probe_media(out).width, probe_media(out).height) == (1080, 1920)
    assert _share(_frame(out, 0.5), (0, 0, 255)) > 0.999


def test_a_crop_with_another_aspect_is_fitted_with_bars_like_any_clip(root):
    out = _render(root, _one_clip(crop=RIGHT_HALF), "bars.mp4", width=360, height=640)
    frame = _frame(out, 1.0)
    assert _share(frame[:, :, :], (255, 0, 0)) == 0.0  # no red at all
    assert _share(frame[:110], (0, 0, 0), tol=30) > 0.999 and _share(frame[-110:], (0, 0, 0), tol=30) > 0.999
    assert (
        _share(frame[125:515], (0, 0, 255)) > 0.999
    )  # 320x360 fitted to 360 wide: 405 rows (117..522), bars above and below


def test_no_crop_still_shows_the_whole_source(root):
    out = _render(root, _one_clip(), "whole.mp4")
    assert (probe_media(out).width, probe_media(out).height) == (640, 360)  # the first clip's size, as before
    frame = _frame(out, 1.0)
    assert _share(frame[:, :300], (255, 0, 0)) > 0.98 and _share(frame[:, 340:], (0, 0, 255)) > 0.99


def test_the_left_half_crop_is_red_with_the_marker_at_its_centre(root):
    out = _render(root, _one_clip(crop={"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}), "left.mp4", width=320, height=360)
    frame = _frame(out, 1.0)
    assert _share(frame, (255, 0, 0)) > 0.97 and _share(frame[170:190, 150:170], (255, 255, 255)) > 0.99


def test_an_overlay_clip_can_be_cropped(root):
    p = _one_clip("green.mp4")
    p, track = timeline_ops.add_track(p, "video")
    p, clip = timeline_ops.add_clip(p, track.id, "split.mp4", 2.0, start=0.0)
    p = timeline_ops.set_clip_properties(p, clip.id, crop=RIGHT_HALF, scale=0.5, x=0.5, y=0.5)
    out = _render(root, p, "overlay.mp4")
    frame = _frame(out, 1.0)  # 640x360: the overlay is 320 wide (columns 160..480), centred
    assert _share(frame[:, 170:470], (0, 0, 255)) > 0.99
    assert _share(frame, (255, 0, 0)) == 0.0
    assert _share(frame[:, :150], (0, 255, 0)) > 0.99 and _share(frame[:, 490:], (0, 255, 0)) > 0.99  # green around it


def test_a_crossfade_between_a_cropped_and_an_uncropped_clip_renders(root):
    p = empty_project()
    tid = p.tracks[0].id
    p, a = timeline_ops.add_clip(p, tid, "split.mp4", 2.0)
    p = timeline_ops.set_clip_properties(p, a.id, crop=RIGHT_HALF)
    p, b = timeline_ops.add_clip(p, tid, "split.mp4", 2.0)
    p = timeline_ops.set_transition(p, b.id, "crossfade", 0.5)
    p, c = timeline_ops.add_clip(p, tid, "split.mp4", 2.0)  # a cut after the fade: the SPL-109 timebase path
    out = _render(root, p, "fade.mp4", width=360, height=640)
    assert probe_duration(out) == pytest.approx(5.5, abs=0.3)
    assert _share(_frame(out, 0.5), (255, 0, 0)) == 0.0  # the cropped clip alone: no red
    assert _share(_frame(out, 3.0), (255, 0, 0)) > 0.1  # the uncropped clip: its red half is back


def test_a_plan_with_a_crop_applies_and_renders(root):
    media, store = LocalMediaStore(root), LocalProjectStore(root)
    asset = timeline_service.assets.register_path(media, "split.mp4").id
    crop = timeline_ops.crop_for_aspect(640, 360, 9, 16, center_x=0.75)
    plan = {
        "schema_version": 1,
        "project": "viaplan",
        "tracks": [{"kind": "video", "clips": [{"asset": asset, "in": 0, "out": 2, "crop": crop}]}],
    }
    edit_plan.apply_plan(media, store, plan)
    project, filename, out_id = timeline_service.plan_render(store, media, "viaplan", "plan.mp4", True, 360, 640)
    timeline_service.run_render(project, media, out_id, width=360, height=640)
    frame = _frame(root / filename, 1.0)
    assert frame.shape[:2] == (640, 360) and _share(frame, (0, 0, 255)) > 0.999


def test_ops_set_a_crop_that_renders(root):
    media, store = LocalMediaStore(root), LocalProjectStore(root)
    timeline_service.create_project(store, "viaops")
    timeline_service.add_clip(store, media, "viaops", path="split.mp4")
    clip = store.load_timeline("viaops").tracks[0].clips[0].id
    crop = timeline_ops.crop_for_aspect(640, 360, 9, 16, center_x=0.75)
    edit_ops.apply_ops(
        media,
        store,
        {"schema_version": 1, "project": "viaops", "ops": [{"op": "set_clip_properties", "clip": clip, "crop": crop}]},
    )
    project, filename, out_id = timeline_service.plan_render(store, media, "viaops", "ops.mp4", True, 360, 640)
    timeline_service.run_render(project, media, out_id, width=360, height=640)
    assert _share(_frame(root / filename, 1.0), (0, 0, 255)) > 0.999


def test_bad_sizes_are_rejected_by_render(root):
    with pytest.raises(TimelineError):
        render(_one_clip(), LocalMediaStore(root), "never.mp4", width=361, height=640)
    assert not (root / "never.mp4").exists()


def test_the_cli_crops_by_aspect_and_renders_at_a_size(root):
    runner = CliRunner()

    def run(*args):
        result = runner.invoke(app, ["compose", *args, "--root", str(root)])
        assert result.exit_code == 0, result.output
        return result

    run("new", "cli")
    run("add-clip", "cli", "split.mp4")
    shown = json.loads(run("crop", "cli", "1", "--aspect", "9:16", "--center-x", "0.75", "--json").output)
    crop = shown["tracks"][0]["clips"][0]["crop"]
    assert (
        crop["h"] == 1.0
        and crop["x"] == pytest.approx(0.592, abs=1e-3)
        and crop["w"] == pytest.approx(0.3164, abs=1e-3)
    )
    zoomed = json.loads(
        run("crop", "cli", "1", "--aspect", "9:16", "--center-x", "0.75", "--zoom", "2", "--json").output
    )
    assert zoomed["tracks"][0]["clips"][0]["crop"]["h"] == pytest.approx(0.5)
    explicit = json.loads(run("crop", "cli", "1", "--x", "0.5", "--y", "0", "--w", "0.5", "--h", "1", "--json").output)
    assert explicit["tracks"][0]["clips"][0]["crop"] == RIGHT_HALF
    assert "crop" in run("show", "cli").output
    json.loads(run("crop", "cli", "1", "--aspect", "9:16", "--center-x", "0.75", "--json").output)
    out = json.loads(run("render", "cli", "--size", "360x640", "--json").output)
    assert _share(_frame(root / out["output"], 1.0), (0, 0, 255)) > 0.999
    assert json.loads(run("crop", "cli", "1", "--clear", "--json").output)["tracks"][0]["clips"][0]["crop"] is None

    bad = runner.invoke(
        app, ["compose", "crop", "cli", "1", "--x", "0.5", "--y", "0", "--w", "0.6", "--h", "1", "--root", str(root)]
    )
    assert bad.exit_code == 1 and "crop must have" in bad.output
    assert runner.invoke(app, ["compose", "crop", "cli", "1", "--x", "0.5", "--root", str(root)]).exit_code != 0
    bad = runner.invoke(app, ["compose", "render", "cli", "--size", "361x640", "--overwrite", "--root", str(root)])
    assert bad.exit_code == 1 and "even" in bad.output
