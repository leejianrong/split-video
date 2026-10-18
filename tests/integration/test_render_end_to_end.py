import subprocess

import pytest

from split_video import timeline_ops as ops
from split_video.ffmpeg import probe_duration, probe_media
from split_video.render import render
from split_video.timeline import TimelineError, empty_project


def _make(path, seconds, color, audio=True):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:size=320x240:rate=25:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


@pytest.fixture
def two_sources(tmp_path):
    _make(tmp_path / "red.mp4", 4, "red")
    _make(tmp_path / "blue.mp4", 3, "blue", audio=False)
    return tmp_path


def _project(root, transition=None):
    p = empty_project()
    tid = p.tracks[0].id
    p, a = ops.add_clip(p, tid, "red.mp4", 4.0)
    p, b = ops.add_clip(p, tid, "blue.mp4", 3.0)
    if transition:
        p = ops.set_transition(p, b.id, "crossfade", transition)
    return p


def test_render_with_cut(two_sources):
    seen = []
    out = two_sources / "out.mp4"
    render(_project(two_sources), two_sources, out, progress=seen.append)
    assert probe_duration(out) == pytest.approx(7.0, abs=0.3)
    assert probe_media(out).has_audio
    assert seen[-1] == 1.0 and seen == sorted(seen)


def test_render_with_crossfade_is_shorter(two_sources):
    out = two_sources / "out.mp4"
    render(_project(two_sources, transition=1.0), two_sources, out)
    assert probe_duration(out) == pytest.approx(6.0, abs=0.3)


def test_render_respects_trim(two_sources):
    p = _project(two_sources)
    clip = p.tracks[0].clips[0]
    p = ops.trim_clip(p, clip.id, 4.0, in_point=1.0, out_point=3.0)
    out = two_sources / "out.mp4"
    render(p, two_sources, out)
    assert probe_duration(out) == pytest.approx(5.0, abs=0.3)


def test_missing_source_and_empty_project_are_clear_errors(two_sources):
    p = _project(two_sources)
    (two_sources / "blue.mp4").unlink()
    with pytest.raises(TimelineError, match="not found"):
        render(p, two_sources, two_sources / "o.mp4")
    with pytest.raises(TimelineError, match="no clips"):
        render(empty_project(), two_sources, two_sources / "o.mp4")


def _frame_rgb(path, t, x, y):
    """The (r, g, b) of one pixel of the frame at time `t`."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-vf", f"format=rgb24,crop=1:1:{x}:{y}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return tuple(out[:3])


def test_overlay_clip_appears_only_in_its_window_and_place(two_sources):
    p = empty_project()
    tid = p.tracks[0].id
    p, _ = ops.add_clip(p, tid, "red.mp4", 4.0)
    p, overlay = ops.add_track(p, "video")
    p, blue = ops.add_clip(p, overlay.id, "blue.mp4", 2.0, start=1.0)
    p = ops.set_clip_properties(p, blue.id, x=0.75, y=0.25, scale=0.25)
    out = two_sources / "out.mp4"
    render(p, two_sources, out)
    assert probe_duration(out) == pytest.approx(4.0, abs=0.3)
    inside, outside = (240, 60), (60, 180)  # centre of the overlay vs. a base-only corner
    red, blue_rgb = (255, 0, 0), (0, 0, 255)

    def close(rgb, want):
        return all(abs(a - b) < 40 for a, b in zip(rgb, want, strict=True))

    assert close(_frame_rgb(out, 2.0, *inside), blue_rgb)
    assert close(_frame_rgb(out, 2.0, *outside), red)
    assert close(_frame_rgb(out, 0.3, *inside), red)  # before the overlay starts
    assert close(_frame_rgb(out, 3.6, *inside), red)  # after it ends


def test_half_opacity_blends_with_the_base(two_sources):
    p = empty_project()
    p, _ = ops.add_clip(p, p.tracks[0].id, "red.mp4", 4.0)
    p, overlay = ops.add_track(p, "video")
    p, blue = ops.add_clip(p, overlay.id, "blue.mp4", 3.0, start=0.0)
    p = ops.set_clip_properties(p, blue.id, opacity=0.5)
    out = two_sources / "out.mp4"
    render(p, two_sources, out)
    r, g, b = _frame_rgb(out, 1.0, 160, 120)
    assert 60 < r < 200 and 60 < b < 200  # a mix of red and blue, neither pure


def test_overlay_only_project_renders_over_black(two_sources):
    p = empty_project()
    p, overlay = ops.add_track(p, "video")
    p, _ = ops.add_clip(p, overlay.id, "blue.mp4", 2.0, start=1.0)
    out = two_sources / "out.mp4"
    render(p, two_sources, out)
    assert probe_duration(out) == pytest.approx(3.0, abs=0.3)
    assert max(_frame_rgb(out, 0.3, 160, 120)) < 40


def _volume(path):
    """(mean_volume, max_volume) in dB as measured by ffmpeg's volumedetect."""
    err = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    ).stderr
    values = {}
    for key in ("mean_volume", "max_volume"):
        line = next(line for line in err.splitlines() if f"{key}:" in line)
        values[key] = float(line.split(f"{key}:")[1].split("dB")[0])
    return values["mean_volume"], values["max_volume"]


def _single(two_sources, **props):
    p = empty_project()
    p, clip = ops.add_clip(p, p.tracks[0].id, "red.mp4", 4.0)
    if props:
        p = ops.set_clip_properties(p, clip.id, **props)
    out = two_sources / "out.mp4"
    render(p, two_sources, out)
    return out


def test_gain_changes_loudness_by_the_expected_amount(two_sources):
    base = _volume(_single(two_sources))[0]
    boosted = _volume(_single(two_sources, gain=2.0))[0]
    assert boosted - base == pytest.approx(6.0, abs=1.0)


def test_muted_clip_is_silent(two_sources):
    assert _volume(_single(two_sources, muted=True))[1] < -60


def test_audio_track_is_mixed_with_the_main_audio(two_sources):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=6", str(two_sources / "music.wav")],
        capture_output=True, check=True,
    )  # fmt: skip
    p = empty_project()
    p, main = ops.add_clip(p, p.tracks[0].id, "red.mp4", 4.0)
    p = ops.set_clip_properties(p, main.id, muted=True)
    p, track = ops.add_track(p, "audio")
    p, _ = ops.add_clip(p, track.id, "music.wav", 6.0, start=1.0)
    out = two_sources / "out.mp4"
    render(p, two_sources, out)
    assert probe_duration(out) == pytest.approx(7.0, abs=0.3)  # the audio track is the longest thing
    assert _volume(out)[0] > -40  # music is audible even though the main clip is muted


def test_brightness_and_saturation_change_the_picture(two_sources):
    _make(two_sources / "grey.mp4", 2, "gray")

    def render_clip(source, **props):
        p = empty_project()
        p, clip = ops.add_clip(p, p.tracks[0].id, source, 2.0)
        if props:
            p = ops.set_clip_properties(p, clip.id, **props)
        out = two_sources / f"{source}-{len(props)}.mp4"
        render(p, two_sources, out)
        return out

    plain = _frame_rgb(render_clip("grey.mp4"), 1.0, 160, 120)
    bright = _frame_rgb(render_clip("grey.mp4", brightness=0.3), 1.0, 160, 120)
    assert bright[0] > plain[0] + 40

    red = _frame_rgb(render_clip("red.mp4", saturation=0.0), 1.0, 160, 120)
    assert max(red) - min(red) < 25  # no longer red
