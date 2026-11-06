"""Server side of the SPL-73 spike: the plan the page draws and the server's frames to compare it with.

    uv run python spikes/browser-compositor/golden.py MEDIA_DIR OUT_DIR

MEDIA_DIR holds main90.mp4 and over90.mp4 (see README.md). Writes OUT_DIR/plan.json (the clips the page draws),
OUT_DIR/golden-*.png (`preview_frame` at the compared times) and OUT_DIR/server-timing.json (how long it takes).
"""

import json
import random
import sys
import time
from pathlib import Path

from split_video import assets
from split_video import timeline_ops as ops
from split_video.local_media_store import LocalMediaStore
from split_video.preview import preview_frame
from split_video.timeline import empty_project

WIDTH = 480
COMPARE = {"plain": 2005.0, "crossfade": 2699.5}  # timeline seconds: overlay + text, and a half-way cross-fade


def build(media):
    main = assets.register_path(media, "main90.mp4")
    over = assets.register_path(media, "over90.mp4")
    p = empty_project()
    track = p.tracks[0].id
    p, _ = ops.add_clip(p, track, "main90.mp4", 5400.0, in_point=0.0, out_point=2700.0, asset=main.id)
    p, b = ops.add_clip(p, track, "main90.mp4", 5400.0, in_point=2700.0, out_point=5400.0, asset=main.id)
    p = ops.set_transition(p, b.id, "crossfade", 1.0)
    p, layer = ops.add_track(p, "video")
    p, pip = ops.add_clip(p, layer.id, "over90.mp4", 4400.0, out_point=1500.0, start=1000.0, asset=over.id)
    p = ops.set_clip_properties(p, pip.id, x=0.75, y=0.3, scale=0.35, opacity=0.7)
    p, titles = ops.add_track(p, "video")
    p, _ = ops.add_text_clip(p, titles.id, "Golden 123", 10.0, start=2000.0, font_size=0.15, y=0.5)
    return p


def plan_of(p):
    """What the page needs from the project: the same clip fields `render.build_plan` reads."""
    out = {"duration": p.duration, "main": [], "layers": []}
    for track in p.tracks:
        for c in track.clips:
            row = {k: getattr(c, k) for k in ("kind", "in_point", "out_point", "start", "x", "y", "scale", "opacity")}
            row.update({k: getattr(c, k) for k in ("text", "font_size", "color", "transition", "transition_duration")})
            row["src"] = c.source
            row["end"] = c.end
            (out["main"] if track.magnetic else out["layers"]).append(row)
    return out


def main(media_dir, out_dir):
    media = LocalMediaStore(Path(media_dir).resolve())
    out = Path(out_dir)
    p = build(media)
    (out / "plan.json").write_text(json.dumps(plan_of(p), indent=1))
    for name, t in COMPARE.items():
        f = preview_frame(p, media, t, width=WIDTH, format="png", source="original")
        (out / f"golden-{name}.png").write_bytes(f.data)
        print(name, f.info())
    rnd = random.Random(7)
    times = [rnd.uniform(0, p.duration - 1) for _ in range(20)]
    ms = []
    for t in times:  # cold: each time is a new seek, as in a scrub
        t0 = time.perf_counter()
        preview_frame(p, media, t, width=WIDTH, format="jpeg", source="original")
        ms.append((time.perf_counter() - t0) * 1000)
    (out / "server-timing.json").write_text(json.dumps({"times": times, "ms": ms}))
    ms.sort()
    print(f"preview_frame in process: median {ms[10]:.0f} ms, p90 {ms[18]:.0f} ms, max {ms[-1]:.0f} ms")


if __name__ == "__main__":
    main(*sys.argv[1:3])
