"""Export and render always read the original, never a proxy."""

import re
import subprocess
from pathlib import Path

from split_video import assets, proxy
from split_video import timeline_ops as ops
from split_video.local_media_store import LocalMediaStore
from split_video.render import render
from split_video.timeline import empty_project

SRC = Path(__file__).resolve().parents[2] / "src" / "split_video"


class Recording(LocalMediaStore):
    def __init__(self, root):
        super().__init__(root)
        self.read: list[str] = []

    def local_path(self, media_id):
        self.read.append(media_id)
        return super().local_path(media_id)


def test_render_reads_only_the_original_even_when_a_proxy_exists(tmp_path):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=3", "-pix_fmt", "yuv420p"]
    assert subprocess.run([*cmd, str(tmp_path / "red.mp4")], capture_output=True, check=False).returncode == 0
    media = Recording(tmp_path.resolve())
    asset = assets.register_path(media, "red.mp4")
    proxy.prepare(media, asset.id)
    assert proxy.visual_source(media, asset.id) != "red.mp4"
    media.read.clear()

    p = empty_project()
    p, _ = ops.add_clip(p, p.tracks[0].id, "red.mp4", 3.0)
    render(p, media, "out.mp4")
    assert media.read and set(media.read) == {"red.mp4"}
    assert (tmp_path / "out.mp4").stat().st_size > 0


def test_the_render_and_export_modules_do_not_know_about_proxies():
    for module in ("render.py", "project.py", "timeline_service.py"):
        assert not re.search(r"\bproxy\b", (SRC / module).read_text()), module
