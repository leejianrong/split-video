"""One frame of the edited result at a time T: what an agent (or the editor) looks at to judge a timeline.

`preview_frame(project, media, t, ...)` renders the real composite (main track, overlays, text, colour and crop)
with the export's own code: `render.prepare` resolves the sources and the frame size, `render.build_plan` writes the
filter graph. The only difference is the window: the project is cut to the one instant (`window`), so ffmpeg seeks
each source to where it is at T and decodes a handful of frames, however long the timeline is. The HTTP route
(`GET /compose/projects/{name}/preview-frame`), the CLI (`split-video compose preview-frame`) and the MCP tool
(`preview_frame`) are thin adapters over it. The image is never written to the workspace.

Time: `t` is rounded down to the output's frame grid (the frame the export has at that time), so `result.time`
is at most one frame before `t`; `t` equal to the duration gives the last frame, later is a `FrameTimeError`.

Size: `width` (never above the export's) scales the whole frame, height follows the export's aspect ratio. The
graph is built at that size, so the frame is what the export would render at that size; at the export's own size
and `source="original"` it is the export's frame up to the encoder's loss (the test sets the tolerance).

Sources: `source="auto"` reads a clip's proxy when it has one (fast on long or high-bitrate files, time right to
within one proxy frame, 1/15 s), `"original"` reads the files like the export, which never reads a proxy.

Not drawn: captions (a render option, see `captions.py`) and audio.
"""

from __future__ import annotations

import math
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import Any

from split_video import frames, presets, proxy, render
from split_video.ffmpeg import ExtractError, _require_binary
from split_video.media_store import MediaStore
from split_video.timeline import Clip, Project, Track

SOURCES = frames.SOURCES
DEFAULT_WIDTH = 640
TIMEOUT = 30.0  # seconds; one frame takes well under a second, this only stops a wedged ffmpeg


@dataclass(frozen=True)
class PreviewFrame:
    data: bytes  # the encoded image
    mime: str
    format: str
    project: str
    revision: int  # of the project the frame was rendered from
    requested_time: float
    time: float  # timeline seconds of the frame shown: requested_time on the output's frame grid
    width: int
    height: int
    source: str  # "proxy", "original" or "mixed": what the clips were read from
    duration: float  # of the timeline

    def info(self) -> dict[str, Any]:
        """Everything but the bytes: the CLI's JSON, the MCP text block and the HTTP headers all say this."""
        return {
            "project": self.project,
            "revision": self.revision,
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


def window(project: Project, t: float, end: float | None = None) -> tuple[Project, float]:
    """The part of `project` that matters at `t`, moved to start at the returned offset `w0` (so `t - w0` is where
    the frame is in the result). Clips that are over or not yet started at `t` are dropped, the others lose the part
    before `w0` (their `in_point` moves up, their `start` down) and audio tracks go. `w0` is `t`, except while two main
    track clips cross-fade: it is then the later one's start, so the fade plays out exactly as in the export.

    With `end` it is the window `[t, end)` for `preview_range.py`: every clip that overlaps it is kept whole after
    its head is cut (the render stops at `end - w0`), and audio tracks stay."""
    main = render._main_track(project)
    w0 = t
    if main is not None:
        active = [c for c in main.clips if c.start <= t < c.end]
        if len(active) > 1:
            w0 = max(c.start for c in active)

    def cut(clip: Clip) -> Clip:
        head = max(0.0, w0 - clip.start)
        return replace(clip, in_point=clip.in_point + head, start=max(0.0, clip.start - w0))

    def keep(c: Clip) -> bool:
        return c.start <= t < c.end if end is None else c.end > w0 and c.start < end

    tracks = [
        replace(track, clips=tuple(cut(c) for c in track.clips if keep(c)))
        for track in project.tracks
        if track.kind == "video" or end is not None
    ]
    return replace(project, tracks=tuple(tracks)), w0


def _even(n: float) -> int:
    return max(2, round(n / 2) * 2)


def use_proxies(media: MediaStore, project: Project, infos: dict[str, Any]) -> tuple[Project, str]:
    """`project` with the media clips pointed at their proxies where there is one, and what was read (see
    `PreviewFrame.source`). The proxies' `MediaInfo` is added to `infos`."""
    kinds: set[str] = set()
    tracks: list[Track] = []
    for track in project.tracks:
        clips = []
        for clip in track.clips:
            if clip.kind == "media" and clip.asset:
                source = proxy.visual_source(media, clip.asset)
                kinds.add("original" if source == clip.source else "proxy")
                if source != clip.source:
                    infos.setdefault(source, media.probe(source))
                    clip = replace(clip, source=source)
            clips.append(clip)
        tracks.append(replace(track, clips=tuple(clips)))
    return replace(project, tracks=tuple(tracks)), kinds.pop() if len(kinds) == 1 else "mixed" if kinds else "original"


def preview_frame(
    project: Project,
    media: MediaStore,
    t: float,
    *,
    name: str = "",
    width: int | None = None,
    preset: str | None = None,
    fit: str | None = None,
    format: str = "jpeg",
    quality: int = frames.DEFAULT_QUALITY,
    source: str = "auto",
) -> PreviewFrame:
    """The composite of `project` at `t` seconds (see the module docstring), `width` pixels wide (default
    `min(640, export width)`). `preset` and `fit` are the export's: they decide the frame's size and how pictures fit.

    Raises `frames.FrameArgumentError` (bad width, format, quality or source), `frames.FrameTimeError` (`t` negative
    or past the end), `TimelineError` for what the export also refuses (no clips, a missing source) and
    `ExtractError` if ffmpeg fails or runs past `TIMEOUT`."""
    fmt = frames.normalize_format(format)
    if source not in SOURCES:
        raise frames.FrameArgumentError(f"unknown source {source!r}: use auto or original")
    if not 1 <= quality <= 100:
        raise frames.FrameArgumentError("quality must be between 1 and 100")
    if width is not None and not 2 <= width <= frames.MAX_DIMENSION:
        raise frames.FrameArgumentError(f"width must be between 2 and {frames.MAX_DIMENSION} pixels")
    if math.isnan(t) or t < 0:
        raise frames.FrameTimeError(f"time must be 0 or more seconds, not {t}")
    preset = presets.canvas_preset(preset, None, None, project.canvas_preset)
    settings = presets.resolve_export(preset, None, None, fit)
    prepared = render.prepare(project, media, settings)
    duration = prepared.project.duration
    if t > duration + frames.END_TOLERANCE:
        raise frames.FrameTimeError(f"time {t:g} s is past the end of the timeline, which is {duration:g} s long")

    fps = settings.fps
    index = min(math.floor(t * fps + 1e-6), max(0, math.ceil(duration * fps - 1e-6) - 1))
    frame_time = index / fps
    out_width = _even(min(width or DEFAULT_WIDTH, prepared.width))
    out_height = _even(out_width * prepared.height / prepared.width)

    windowed, w0 = window(prepared.project, frame_time)
    infos = dict(prepared.infos)
    kind = "original"
    if source == "auto":
        windowed, kind = use_proxies(media, windowed, infos)
    plan = render.build_plan(windowed, infos, out_width, out_height, prepared.font, fps, settings.fit, audio=False)

    if fmt == "jpeg":
        codec = ["-c:v", "mjpeg", "-q:v", str(round(2 + (100 - quality) * 29 / 99)), "-pix_fmt", "yuvj420p"]
    else:
        codec = ["-c:v", "png"]
    seek = frame_time - w0

    with ExitStack() as stack:
        paths = [str(stack.enter_context(media.local_path(c.source))) for c in plan.clips]
        cmd = [
            _require_binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
            *render._clip_inputs(plan.clips, paths),
            "-filter_complex", plan.graph, "-map", f"[{plan.video_label}]",
            *(["-ss", f"{seek:.6f}"] if seek > 0 else []),
            "-frames:v", "1", "-an", *codec, "-f", "image2pipe", "-",
        ]  # fmt: skip
        try:
            result = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, check=False, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ExtractError(
                f"ffmpeg took more than {TIMEOUT:g} s to render the preview frame and was killed"
            ) from None
    if result.returncode != 0 or not result.stdout:
        tail = result.stderr.decode(errors="replace").strip()[-1000:]
        raise ExtractError(f"could not render a preview frame at {frame_time:g} s: {tail}")
    return PreviewFrame(
        data=result.stdout,
        mime=frames.FORMATS[fmt],
        format=fmt,
        project=name,
        revision=project.revision,
        requested_time=t,
        time=round(frame_time, 3),
        width=out_width,
        height=out_height,
        source=kind,
        duration=round(duration, 3),
    )
