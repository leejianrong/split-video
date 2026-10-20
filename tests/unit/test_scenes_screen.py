"""The screen-change rule on real ffmpeg output: synthetic coding-screencast clips (dark text panes, a blinking cursor,
typing, scrolling code) with changes at known times, analysed at the default settings (SPL-107).

The measured numbers behind the defaults are in the `scenes.py` docstring."""

import subprocess
import sys
from pathlib import Path

import pytest

from split_video import assets, scenes
from split_video.local_media_store import LocalMediaStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screen_clip import mono_font

pytestmark = pytest.mark.skipif(mono_font() is None, reason="needs the DejaVu Sans Mono font for drawtext")

BG = "0x1e1e1e"
PANE_A = ["import os, sys", "def cut_burst(frames, limit):", "    best = max(frames, key=score)", "    return best"]
PANE_B = [
    "def render_timeline(project):",
    "    for clip in project.clips:",
    "        probe(clip.asset)",
    "    return 1",
]
CURSOR = "drawbox=x=60:y=640:w=14:h=30:color=gray:t=fill:enable='lt(mod(t,1),0.5)'"
TYPED = "result = detect_screen_changes(frames, threshold)"


def _text(lines, colour="0xd4d4d4", y0=60):
    return ",".join(
        f"drawtext=fontfile={mono_font()}:text='{line}':fontcolor={colour}:fontsize=30:x=60:y={y0 + 48 * i}"
        for i, line in enumerate(lines)
    )


def _segment(path, background, vf, seconds):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={background}:s=1280x720:r=10:d={seconds}"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", "20", str(path)]
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def _concat(path, parts):
    listing = path.with_suffix(".txt")
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(path)]
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    return tmp_path_factory.mktemp("screen")


def _analyse(directory, name, **kwargs):
    media = LocalMediaStore(directory.resolve())
    asset = assets.register_path(media, name)
    scenes.analyze(media, asset.id, **kwargs)
    return scenes.load(media, asset.id)


@pytest.fixture(scope="module")
def swap_clip(workdir):
    """A 20 s dark editor with a blinking cursor; at 8 s a second dark text pane (same background) replaces the
    first; at 14 s a light slide (a hard cut)."""
    a, b, c = (workdir / f"{n}.mp4" for n in "abc")
    _segment(a, BG, _text(PANE_A) + "," + CURSOR, 8)
    _segment(b, BG, _text(PANE_B) + "," + CURSOR, 6)
    _segment(c, "0xf2f2f2", _text(["Quarterly Review"], "black", 300), 6)
    _concat(workdir / "swap.mp4", [a, b, c])
    return workdir


def test_a_dark_pane_swap_is_found_as_a_screen_change_and_the_cursor_is_not(swap_clip):
    doc = _analyse(swap_clip, "swap.mp4")
    assert doc["version"] == 2 and doc["screen_threshold"] == scenes.DEFAULT_SCREEN_THRESHOLD
    found = [(s["time"], s["kind"]) for s in doc["scenes"]]
    assert [k for _, k in found] == ["screen", "cut"], found
    assert abs(found[0][0] - 8.0) <= 0.5 and abs(found[1][0] - 14.0) <= 0.5
    swap = doc["scenes"][0]
    assert swap["score"] < 0.05 and 0.02 <= swap["changed"] <= 0.2  # scdet cannot see it: 0.01
    assert doc["scenes"][1]["changed"] > 0.9


def test_scdet_alone_misses_the_pane_swap_which_is_why_the_rule_exists(swap_clip):
    doc = _analyse(swap_clip, "swap.mp4", screen_threshold=0, force=True)
    assert [s["kind"] for s in doc["scenes"]] == ["cut"]  # only the slide
    assert all("changed" not in s for s in doc["scenes"]) and doc["screen_threshold"] == 0
    assert _analyse(swap_clip, "swap.mp4", threshold=0.005, screen_threshold=0, force=True)["scenes"][0]["time"] < 9.0


def test_typing_and_a_blinking_cursor_do_not_fire_at_any_default(workdir):
    typed = ",".join(
        f"drawtext=fontfile={mono_font()}:text='{TYPED[:k]}':fontcolor=0xd4d4d4:fontsize=30:x=60:y=400:"
        f"enable='between(t,{k * 0.2:.1f},{(k + 1) * 0.2:.1f})'"
        for k in range(1, len(TYPED) + 1)
    )
    _segment(workdir / "typing.mp4", BG, _text(PANE_A) + "," + typed + "," + CURSOR, 9)
    doc = _analyse(workdir, "typing.mp4")
    assert doc["scenes"] == []


def test_scrolling_code_is_continuous_activity_not_screen_changes(workdir):
    lines = ",".join(
        f"drawtext=fontfile={mono_font()}:text='    line {i} = compute(value, {i * 7})':fontcolor=0xd4d4d4:"
        f"fontsize=30:x=60:y='900-mod(t*120+{i * 48},1000)'"
        for i in range(24)
    )
    still = workdir / "still.mp4"
    _segment(still, BG, _text(PANE_A), 3)
    scroll = workdir / "scroll_only.mp4"
    _segment(scroll, BG, lines, 6)
    _concat(workdir / "scroll.mp4", [still, scroll, still])  # it starts and it stops: neither edge fires
    doc = _analyse(workdir, "scroll.mp4")
    assert doc["scenes"] == [], doc["scenes"]  # the picture changes at 3 s and 9 s, but never in one isolated frame
    assert max(doc["activity"][4:8]) > 0.5  # the scroll itself is plainly visible as activity
