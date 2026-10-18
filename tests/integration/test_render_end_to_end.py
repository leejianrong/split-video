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
