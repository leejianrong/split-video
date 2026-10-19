"""A synthetic screen recording with text that is known exactly (for the OCR tests, SPL-98).

1920x1080 at 5 fps, 12 s per scene, three scenes with cuts at 12 s and 24 s:
  0-12 s  an editor: white monospace code on a dark background
  12-24 s a mid-grey terminal: a red failing-test line and a white assertion line
  24-36 s a slide: large dark text on a light background
`SCENES` lists each scene's lines; `PLANTED` is the identifier the search tests look for and when it is on screen.
"""

from __future__ import annotations

import os
import subprocess

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
]
PLANTED = "test_cut_burst_keeps_strongest"
PLANTED_AT = (12.0, 24.0)  # on screen from 12 s to 24 s

# (background, [(text, colour, font size)]) per scene
SCENES = [
    (
        "0x1e1e2e",
        [
            ("# split_video/scenes.py", "white", 30),
            ("def merge_cuts(scenes, min_gap):", "white", 30),
            ("    kept = strongest_of(scenes)", "white", 30),
            ("    return sorted(kept, key=start_time)", "white", 30),
        ],
    ),
    (
        "0x585870",  # a mid-dark grey terminal: a red line is close to it in luma, far in the red channel
        [
            ("$ make test", "white", 30),
            (f"FAILED tests/unit/test_scenes.py::{PLANTED} - AssertionError", "0xff5555", 30),
            ("E       AssertionError: assert 3 == 2", "white", 30),
        ],
    ),
    (
        "0xf5f5f0",
        [
            ("Building a video editor for agents", "0x202020", 72),
            ("Chapter two: silence detection", "0x202020", 72),
        ],
    ),
]
SCENE_SECONDS = 12.0
CUTS = [SCENE_SECONDS * k for k in range(1, len(SCENES))]
DURATION = SCENE_SECONDS * len(SCENES)


def mono_font() -> str | None:
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _drawtext(text: str, colour: str, size: int, line: int, font: str) -> str:
    safe = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")
    return f"drawtext=fontfile={font}:text='{safe}':fontcolor={colour}:fontsize={size}:x=80:y={120 + line * int(size * 1.9)}"


def screen_clip(path, fps: int = 5) -> None:
    """Write the clip to `path` (needs ffmpeg with drawtext and a monospace font: see `mono_font`)."""
    font = mono_font()
    assert font, "no DejaVu Sans Mono font installed"
    cmd = ["ffmpeg", "-y", "-v", "error"]
    parts = []
    for k, (background, lines) in enumerate(SCENES):
        cmd += ["-f", "lavfi", "-i", f"color=c={background}:s=1920x1080:r={fps}:d={SCENE_SECONDS}"]
        chain = ",".join(_drawtext(t, c, s, i, font) for i, (t, c, s) in enumerate(lines))
        parts.append(f"[{k}:v]{chain}[s{k}]")
    labels = "".join(f"[s{k}]" for k in range(len(SCENES)))
    graph = ";".join(parts) + f";{labels}concat=n={len(SCENES)}:v=1:a=0,format=yuv420p[o]"
    cmd += ["-filter_complex", graph, "-map", "[o]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    cmd += ["-g", "25", str(path)]
    done = subprocess.run(cmd, capture_output=True, check=False)
    assert done.returncode == 0, done.stderr.decode()[-800:]
