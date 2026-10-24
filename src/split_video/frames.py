"""One still image of an asset at a time T: what an agent (or a person) looks at to judge an edit.

`extract_frame(media, asset_id, time, ...)` returns the encoded image (JPEG or PNG) plus where it came from. The
HTTP route, the CLI (`split-video frame`) and the MCP tool (`get_frame`) are thin adapters over it. The image is
never written to the library or the workspace: ffmpeg writes it to a pipe and the caller gets the bytes.

Which source is read
--------------------
`source="auto"` (the default) reads `proxy.visual_source`: the asset's proxy if a current one exists, else the
original. A proxy is at most 640 px wide and at most 15 fps, so such a frame is small and its time is only right to
within one proxy frame (1/15 s); the original is as sharp as the file and exact to its own frame. Ask for
`source="original"` when the detail matters (reading small text on a screen recording, judging a crop). On a
multi-hour original this is still quick: the seek is input-side (`-ss` before `-i`), which jumps to the nearest
keyframe before T and decodes forward from there, so it costs the length of one GOP, not the position in the file.
The result says which was read (`source`).

Time
----
`time` is seconds from the start of the asset. The frame returned is the first one at or after `time` (ffmpeg's
accurate seek discards earlier frames), so `result.time` is `time` plus up to one frame period. A time from 0 to
the asset's duration is valid; a request exactly at the duration (or a hair inside the last frame's period)
returns the last frame, and anything later is a `FrameTimeError` that states the duration.

Size
----
`width` and/or `height` (pixels, at most `MAX_DIMENSION`) scale the picture keeping its aspect ratio: one alone
fixes that side, both fit the picture inside the box. A picture is never scaled up: asking for more than the
source has returns the source's size (use `source="original"` for a larger frame). Odd sizes are fine.

`quality` (1..100, default 85) is the JPEG quality; PNG is lossless and ignores it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from split_video import assets, proxy
from split_video.ffmpeg import ExtractError, _require_binary, run_cancellable
from split_video.jobs import CancelToken
from split_video.media_store import MediaStore

MAX_DIMENSION = 4096
DEFAULT_QUALITY = 85
FORMATS = {"jpeg": "image/jpeg", "png": "image/png"}
SOURCES = ("auto", "original")
END_TOLERANCE = 0.001  # seconds past the duration that still count as "at the end" (probe rounding)
# When nothing decodes at a time this close to the end (the last frame starts before the duration), seek a bit
# earlier until a frame comes out.
_END_RETRIES = (0.1, 0.5, 1.5)
_END_WINDOW = _END_RETRIES[-1]

_SHOWINFO = re.compile(r"showinfo[^\n\r]*? n:\s*0 [^\n\r]*?pts_time:([\d.]+)[^\n\r]*? s:(\d+)x(\d+)")


class FrameError(ValueError):
    """A frame can't be extracted (the base of the errors below)."""


class FrameArgumentError(FrameError):
    """A bad format, source, quality or size."""


class FrameTimeError(FrameError):
    """The time is negative or past the end of the asset."""


class FrameAudioOnlyError(FrameError):
    """The asset has no picture."""


@dataclass(frozen=True)
class Frame:
    data: bytes  # the encoded image
    mime: str  # "image/jpeg" or "image/png"
    format: str  # "jpeg" or "png"
    asset: str
    requested_time: float
    time: float  # seconds of the frame actually returned (requested_time plus up to one frame period)
    width: int
    height: int
    source: str  # "proxy" or "original": what was read
    duration: float  # of the asset

    def info(self) -> dict[str, object]:
        """Everything but the bytes (what the HTTP headers, the CLI's `--json` and the MCP text block say)."""
        return {
            "asset": self.asset,
            "time": self.time,
            "requested_time": self.requested_time,
            "width": self.width,
            "height": self.height,
            "mime": self.mime,
            "format": self.format,
            "source": self.source,
            "duration": self.duration,
            "bytes": len(self.data),
        }


def normalize_format(name: str) -> str:
    key = name.strip().lower()
    key = "jpeg" if key == "jpg" else key
    if key not in FORMATS:
        raise FrameArgumentError(f"unknown image format {name!r}: use jpeg or png")
    return key


def _scale_filter(width: int | None, height: int | None) -> str | None:
    # min(...) with the source's own size is what stops an upscale; `-1` keeps the aspect ratio.
    if width is not None and height is not None:
        return f"scale=w=min({width}\\,iw):h=min({height}\\,ih):force_original_aspect_ratio=decrease"
    if width is not None:
        return f"scale=min({width}\\,iw):-1"
    if height is not None:
        return f"scale=-1:min({height}\\,ih)"
    return None


def _command(
    ffmpeg: str, source: str, seek: float, fmt: str, quality: int, width: int | None, height: int | None
) -> list[str]:
    filters = [f for f in (_scale_filter(width, height),) if f]
    filters += ["setsar=1", "showinfo"]
    if fmt == "jpeg":
        filters.append("format=yuvj420p")
        codec = ["-c:v", "mjpeg", "-q:v", str(round(2 + (100 - quality) * 29 / 99))]
    else:
        codec = ["-c:v", "png"]
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "info",
        "-ss",
        f"{seek:.3f}",
        "-i",
        source,
        "-an",
        "-sn",
        "-frames:v",
        "1",
        "-vf",
        ",".join(filters),
        *codec,
        "-f",
        "image2pipe",
        "-",
    ]


def extract_frame(
    media: MediaStore,
    asset_id: str,
    time: float,
    *,
    width: int | None = None,
    height: int | None = None,
    format: str = "jpeg",
    quality: int = DEFAULT_QUALITY,
    source: str = "auto",
    cancel: CancelToken | None = None,
) -> Frame:
    """The frame of the asset at `time` seconds (see the module docstring for the source, time and size rules).

    Raises `FrameArgumentError` (bad format, source, quality or size), `FrameTimeError` (negative, or past the
    end: the message states the duration), `FrameAudioOnlyError`, `AssetNotFoundError` / `SourceNotFoundError`
    for a missing asset, `ExtractError` if ffmpeg fails and `JobCancelledError` through `cancel`."""
    fmt = normalize_format(format)
    if source not in SOURCES:
        raise FrameArgumentError(f"unknown source {source!r}: use auto or original")
    if not 1 <= quality <= 100:
        raise FrameArgumentError("quality must be between 1 and 100")
    for name, value in (("width", width), ("height", height)):
        if value is not None and not 1 <= value <= MAX_DIMENSION:
            raise FrameArgumentError(f"{name} must be between 1 and {MAX_DIMENSION} pixels")
    if math.isnan(time) or time < 0:
        raise FrameTimeError(f"time must be 0 or more seconds, not {time}")
    asset = assets.get_asset(media, asset_id)
    if not asset.has_video:
        raise FrameAudioOnlyError(f"asset {asset.id} is audio-only: it has no picture to extract")
    if time > asset.duration + END_TOLERANCE:
        raise FrameTimeError(f"time {time:g} s is past the end of the asset, which is {asset.duration:g} s long")
    ffmpeg = _require_binary("ffmpeg")
    source_id = asset.path if source == "original" else proxy.visual_source(media, asset.id)
    kind = "original" if source_id == asset.path else "proxy"
    seeks = [time]
    if time >= asset.duration - _END_WINDOW:
        seeks += [max(0.0, time - back) for back in _END_RETRIES]
    with media.local_path(source_id) as path:
        for seek in seeks:
            result = run_cancellable(_command(ffmpeg, str(path), seek, fmt, quality, width, height), cancel, text=False)
            if result.returncode == 0 and result.stdout:
                break
        else:
            tail = result.stderr.decode(errors="replace").strip().splitlines()[-3:]
            if result.returncode == 0:
                raise ExtractError(f"no frame at {time:g} s of {asset.path}")
            raise ExtractError(f"could not extract a frame at {time:g} s of {asset.path}: " + " ".join(tail))
    found = _SHOWINFO.search(result.stderr.decode(errors="replace"))
    if found:
        actual, out_width, out_height = seek + float(found.group(1)), int(found.group(2)), int(found.group(3))
    else:  # showinfo's line changed format: still a frame, just without the details
        actual, out_width, out_height = time, 0, 0
    return Frame(
        data=result.stdout,
        mime=FORMATS[fmt],
        format=fmt,
        asset=asset.id,
        requested_time=time,
        time=round(actual, 3),
        width=out_width,
        height=out_height,
        source=kind,
        duration=asset.duration,
    )
