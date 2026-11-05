"""Golden-frame harness (SPL-74): the preview's frame against the export's frame at the same time.

`measure` renders one instant both ways at the same size (so there is no resampling noise) and returns the
differences; `Tolerance.failures` says which limits a measurement breaks. The preview is a lossless PNG of the filter
graph's output, the export's frame went through libx264 (default crf 23), so the two differ by the encoder's loss.
`docs/compose.md` says what this catches and what it cannot.
"""

import subprocess
from dataclasses import dataclass

import numpy as np
from test_frames import decode

from split_video.preview import preview_frame
from split_video.render import render

SIZE = (480, 270)
FPS = 30
HARD_TIMEOUT = 120  # seconds for one ffmpeg run; a wedged ffmpeg must not hold CI (SPL-110)


def make_source(path, lavfi):
    """A deterministic H.264 clip from an ffmpeg lavfi graph (no committed binaries)."""
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", lavfi, "-pix_fmt", "yuv420p"]
    cmd += ["-c:v", "libx264", "-g", "30", str(path)]
    subprocess.run(cmd, capture_output=True, check=True, timeout=HARD_TIMEOUT)


def export(project, media, name):
    render(project, media, name, width=SIZE[0], height=SIZE[1])


def export_frame(media, name, at):
    """The export's frame at `at` seconds as an int array (height, width, rgb)."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{at}", "-i", str(media.root / name)]
    cmd += ["-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True, timeout=HARD_TIMEOUT).stdout
    return decode(out)[2].astype(int)


@dataclass(frozen=True)
class Tolerance:
    mean: float  # mean absolute difference per channel (of 255)
    share: float  # most of the samples that may differ by more than `pixel`
    pixel: int = 24
    separation: float | None = None  # the match must be this many times closer than the frame 0.2 s away
    late_frame: bool = False  # the export may show the incoming clip one frame late after a cross-fade (SPL-123)

    def failures(self, m: "Measurement") -> list[str]:
        out = []
        if m.mean > self.mean:
            out.append(f"mean difference {m.mean:.2f} is over {self.mean}")
        if m.share > self.share:
            out.append(f"{m.share:.2%} of the samples differ by more than {self.pixel}, over {self.share:.2%}")
        if self.separation and m.mean * self.separation >= m.away:
            out.append(f"not {self.separation}x closer than the frame 0.2 s away (mean {m.mean:.2f} v {m.away:.2f})")
        return out


@dataclass(frozen=True)
class Measurement:
    mean: float
    share: float
    away: float  # mean difference to the export's frame 0.2 s off, for the separation check
    time: float

    def __str__(self):
        return f"t={self.time:.3f} mean={self.mean:.2f} share={self.share:.4f} away={self.away:.2f}"


def measure(project, media, name, at, tol: Tolerance) -> Measurement:
    """Preview of `project` at `at` against frame `at` of the export `name` (made earlier by `export`, possibly from
    another project: that is how the mutation tests make the two disagree)."""
    frame = preview_frame(project, media, at, width=SIZE[0], format="png", source="original")
    assert (frame.width, frame.height) == SIZE
    mine = decode(frame.data)[2].astype(int)
    steps = (0, 1) if tol.late_frame else (0,)
    candidates = [(np.abs(mine - export_frame(media, name, frame.time + k / FPS)), k) for k in steps]
    best, _ = min(candidates, key=lambda c: c[0].mean())
    off = 0.2 if frame.time + 0.2 < frame.duration - 0.1 else -0.2
    away = np.abs(mine - export_frame(media, name, frame.time + off))
    return Measurement(best.mean(), float((best > tol.pixel).mean()), away.mean(), frame.time)
