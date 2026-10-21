"""Render a timeline `Project` to a single video file with ffmpeg.

The picture is built bottom-up on a black canvas as long as the project:

1. The main track (the first video track, when it is magnetic) is normalised
   to the first clip's size, frame rate and audio format and joined pairwise
   with `concat` for cuts or `xfade`/`acrossfade` for crossfades.
2. Every clip on any other video track is overlaid at its own start time with
   its own position, size and opacity, in track order (later tracks on top).
3. A cropped clip (`Clip.crop`, fractions of the source frame) is cut to its region first, in source
   pixels, before it is scaled to fit the frame or overlaid; it then fits like any other clip.
   Clips with a colour adjustment get ffmpeg's `eq` filter (brightness,
   contrast, saturation), before padding or overlaying.
4. Audio: each clip's sound (the main track's already joined) gets its gain
   (muted clips contribute silence), is delayed to its start time, and all of
   it is summed with `amix` at unchanged levels.

Each clip is its own `-ss/-t` input, so ffmpeg only decodes the part used.
`build_filter_graph` is pure (it only needs probed media info) so the graph can
be unit tested without ffmpeg; `render` probes, runs ffmpeg and reports
progress.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from split_video import assets, presets
from split_video.ffmpeg import ExtractError, MediaInfo, StallWatchdog, _require_binary, stall_seconds, stop_on_cancel
from split_video.jobs import CancelToken, JobCancelledError
from split_video.media_store import MediaStore
from split_video.timeline import Clip, Project, TimelineError, Track

FPS = presets.DEFAULT_FPS
AUDIO_RATE = 48000
DEFAULT_SIZE = (1280, 720)  # canvas when the project has no video file to take a size from

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


def find_font() -> str:
    """A TrueType font file for text clips: `$SPLIT_VIDEO_FONT` if set, else the first common one."""
    candidates = [os.environ["SPLIT_VIDEO_FONT"]] if os.environ.get("SPLIT_VIDEO_FONT") else []
    for path in [*candidates, *_FONT_CANDIDATES]:
        if os.path.isfile(path):
            return path
    raise TimelineError(
        "no font found for text clips; install a TrueType font (e.g. fonts-dejavu-core) "
        "or point SPLIT_VIDEO_FONT at one"
    )


def _quote(text: str) -> str:
    """`text` as one single-quoted ffmpeg filter option value."""
    return "'" + text.replace("'", "'\\''") + "'"


@dataclass(frozen=True)
class Plan:
    """The ffmpeg inputs and filter graph for a project."""

    clips: list[Clip]  # media clips, one per `-i`, in input order
    graph: str
    video_label: str
    audio_label: str
    width: int
    height: int
    fps: int = FPS


def _main_track(project: Project) -> Track | None:
    first = next((t for t in project.tracks if t.kind == "video"), None)
    return first if first is not None and first.magnetic and first.clips else None


def _audio_clips(project: Project) -> list[Clip]:
    clips: list[Clip] = []
    for track in project.tracks:
        if track.kind == "audio":
            clips.extend(sorted(track.clips, key=lambda c: c.start))
    return clips


def _overlay_clips(project: Project, main: Track | None) -> list[Clip]:
    clips: list[Clip] = []
    for track in project.tracks:
        if track.kind == "video" and track is not main:
            clips.extend(sorted(track.clips, key=lambda c: c.start))
    return clips


def _fmt_audio() -> str:
    return f"aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"


def _eq(clip: Clip) -> str:
    """The colour-adjustment filter for a clip, with its leading comma, or
    nothing when every value is at its neutral setting."""
    if (clip.brightness, clip.contrast, clip.saturation) == (0.0, 1.0, 1.0):
        return ""
    return f",eq=brightness={clip.brightness:g}:contrast={clip.contrast:g}:saturation={clip.saturation:g}"


def _even(n: float) -> int:
    return max(2, round(n / 2) * 2)


def _crop(clip: Clip, info: MediaInfo) -> tuple[str, int, int]:
    """The `crop` filter (with its leading comma, or nothing) for a clip and the size of the picture after it.

    The fractions are applied to the source's pixel size and rounded to even numbers (yuv420p), staying inside
    the frame."""
    iw, ih = info.width or 0, info.height or 0
    if not clip.cropped or not iw or not ih:
        return "", iw, ih
    cw, ch = min(_even(iw * clip.crop_w), iw // 2 * 2), min(_even(ih * clip.crop_h), ih // 2 * 2)
    cx = min(_even(iw * clip.crop_x) if clip.crop_x else 0, iw - cw)
    cy = min(_even(ih * clip.crop_y) if clip.crop_y else 0, ih - ch)
    return f",crop={cw}:{ch}:{cx}:{cy}", cw, ch


def _fit(cw: int, ch: int, width: int, height: int, cropped: bool, fit: str = "letterbox") -> str:
    """The scale-to-fit filter. With `fit="crop"` an uncropped picture is scaled to cover the frame and the overflow
    is cut off, centered, so there are no bars. A cropped picture whose aspect matches the frame to within the rounding of
    the crop (2 source pixels: it was rounded to even sizes) fills it exactly, a barely visible stretch,
    instead of leaving a bar a pixel or two wide."""
    if fit == "crop" and not cropped:
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    if cropped and cw and ch:
        ratio = min(width / cw, height / ch)
        if abs(cw - width / ratio) <= 2 and abs(ch - height / ratio) <= 2:
            return f"scale={width}:{height}"
    return f"scale={width}:{height}:force_original_aspect_ratio=decrease"


def _gain(clip: Clip) -> str:
    return "0" if clip.muted else f"{clip.gain:g}"


def _main_chain(
    clips: list[Clip], first_input: int, infos: dict[str, MediaInfo], width: int, height: int, fps: int, fit: str
):
    """Filter parts joining the main track's clips; returns (parts, vlabel, alabel)."""
    parts: list[str] = []
    for n, clip in enumerate(clips):
        i = first_input + n
        crop, cw, ch = _crop(clip, infos[clip.source])
        parts.append(
            f"[{i}:v]setpts=PTS-STARTPTS,fps={fps}{crop},"
            f"{_fit(cw, ch, width, height, clip.cropped, fit)}{_eq(clip)},"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v{n}]"
        )
        if infos[clip.source].has_audio:
            parts.append(
                f"[{i}:a]asetpts=PTS-STARTPTS,{_fmt_audio()},atrim=0:{clip.duration:.3f},volume={_gain(clip)}[a{n}]"
            )
        else:
            parts.append(f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=0:{clip.duration:.3f},asetpts=PTS-STARTPTS[a{n}]")

    vlabel, alabel = "v0", "a0"
    length = clips[0].duration
    joined_by_cut = False  # concat outputs picture in ffmpeg's 1/1000000 timebase, not the clips' 1/fps
    for n in range(1, len(clips)):
        clip = clips[n]
        nv, na = f"vj{n}", f"aj{n}"
        if clip.transition == "crossfade" and clip.transition_duration > 0:
            d = clip.transition_duration
            if joined_by_cut:
                # xfade refuses inputs with different timebases ("Error reinitializing filters").
                parts.append(f"[{vlabel}]settb=1/{fps}[vt{n}]")
                vlabel, joined_by_cut = f"vt{n}", False
            parts.append(f"[{vlabel}][v{n}]xfade=transition=fade:duration={d:.3f}:offset={length - d:.3f}[{nv}]")
            parts.append(f"[{alabel}][a{n}]acrossfade=d={d:.3f}[{na}]")
            length += clip.duration - d
        else:
            parts.append(f"[{vlabel}][v{n}]concat=n=2:v=1:a=0[{nv}]")
            parts.append(f"[{alabel}][a{n}]concat=n=2:v=0:a=1[{na}]")
            joined_by_cut = True
            length += clip.duration
        vlabel, alabel = nv, na
    return parts, vlabel, alabel


def build_plan(
    project: Project,
    infos: dict[str, MediaInfo],
    width: int,
    height: int,
    font: str | None = None,
    fps: int = FPS,
    fit: str = "letterbox",
) -> Plan:
    main = _main_track(project)
    main_clips = list(main.clips) if main else []
    layers = _overlay_clips(project, main)  # picture clips and text, in stacking order
    overlays = [c for c in layers if c.kind == "media"]  # the ones that need an ffmpeg input
    audio_clips = _audio_clips(project)
    total = project.duration
    ordered = [*main_clips, *overlays, *audio_clips]

    parts: list[str] = []
    if main_clips:
        chain, vmain, alabel = _main_chain(main_clips, 0, infos, width, height, fps, fit)
        parts += chain
    else:
        alabel = "asilent"
        parts.append(f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS[{alabel}]")

    parts.append(f"color=c=black:s={width}x{height}:r={fps}:d={total:.3f},format=yuv420p[canvas]")
    vlabel = "canvas"
    if main_clips:
        parts.append(f"[canvas][{vmain}]overlay=0:0:eof_action=pass[vbase]")
        vlabel = "vbase"

    media_seen = 0
    for n, clip in enumerate(layers):
        if clip.kind == "text":
            if font is None:
                raise TimelineError("text clips need a font; none was given")
            nxt = f"vo{n}"
            parts.append(
                f"[{vlabel}]drawtext=fontfile={_quote(font)}:expansion=none:text={_quote(clip.text)}:"
                f"fontsize={max(8, round(height * clip.font_size))}:fontcolor={clip.color}@{clip.opacity:g}:"
                f"x={width * clip.x:g}-text_w/2:y={height * clip.y:g}-text_h/2:"
                f"enable='between(t,{clip.start:.3f},{clip.end:.3f})'[{nxt}]"
            )
            vlabel = nxt
            continue
        i = len(main_clips) + media_seen
        media_seen += 1
        w = max(2, int(width * clip.scale) // 2 * 2)
        crop = _crop(clip, infos[clip.source])[0]
        if fit == "crop" and not clip.cropped:
            # Cover a box with the frame's aspect (like main-track clips cover the frame), cutting the overflow.
            box_h = max(2, round(w * height / width / 2) * 2)
            scaled = f"scale={w}:{box_h}:force_original_aspect_ratio=increase,crop={w}:{box_h}"
        else:
            scaled = f"scale={w}:-2"
        parts.append(
            f"[{i}:v]setpts=PTS-STARTPTS+{clip.start:.3f}/TB,fps={fps}{crop},{scaled}{_eq(clip)},"
            f"format=yuva420p,colorchannelmixer=aa={clip.opacity:g}[ov{n}]"
        )
        nxt = f"vo{n}"
        parts.append(
            f"[{vlabel}][ov{n}]overlay=x={width * clip.x:g}-w/2:y={height * clip.y:g}-h/2:"
            f"enable='between(t,{clip.start:.3f},{clip.end:.3f})':eof_action=pass:format=auto[{nxt}]"
        )
        vlabel = nxt

    # Sound from everything that isn't the (already joined) main track.
    mix = [alabel]
    for n, clip in enumerate([*overlays, *audio_clips]):
        if clip.muted or not infos[clip.source].has_audio:
            continue
        i = len(main_clips) + n
        delay = round(clip.start * 1000)
        parts.append(
            f"[{i}:a]asetpts=PTS-STARTPTS,{_fmt_audio()},atrim=0:{clip.duration:.3f},"
            f"volume={clip.gain:g},adelay={delay}|{delay}[ax{n}]"
        )
        mix.append(f"ax{n}")
    if len(mix) > 1:
        sources = "".join(f"[{m}]" for m in mix)
        parts.append(f"{sources}amix=inputs={len(mix)}:normalize=0:duration=longest,alimiter=limit=0.95[amixed]")
        alabel = "amixed"

    return Plan(ordered, ";".join(parts), vlabel, alabel, width, height, fps)


MIN_SIZE, MAX_SIZE = 64, 4096


def parse_size(text: str | None) -> tuple[int | None, int | None]:
    """`"1080x1920"` as (1080, 1920); (None, None) for no size. `TimelineError` if it isn't WxH
    (the range is checked by `check_size`)."""
    if text is None:
        return None, None
    parts = text.lower().split("x")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        raise TimelineError(f"size must look like 1080x1920, not {text!r}")
    return int(parts[0]), int(parts[1])


def check_size(width: int | None, height: int | None) -> tuple[int, int] | None:
    """The explicit output size, validated: both or neither, whole even numbers from 64 to 4096.
    Raises `TimelineError`; None (neither given) means "the first clip's size"."""
    if width is None and height is None:
        return None
    if width is None or height is None:
        raise TimelineError("give both width and height, or neither")
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or value % 2 or not MIN_SIZE <= value <= MAX_SIZE:
            raise TimelineError(f"{name} must be an even whole number from {MIN_SIZE} to {MAX_SIZE}")
    return width, height


def _clip_inputs(clips: list[Clip], paths: list[str]) -> list[str]:
    """`-ss/-t/-i` arguments for each clip, reading the matching local path."""
    args: list[str] = []
    for clip, path in zip(clips, paths):
        args += ["-ss", f"{clip.in_point:.3f}", "-t", f"{clip.duration:.3f}", "-i", path]
    return args


def render(
    project: Project,
    media: MediaStore,
    out_id: str,
    progress: Callable[[float], None] | None = None,
    cancel: CancelToken | None = None,
    *,
    width: int | None = None,
    height: int | None = None,
    preset: str | None = None,
    fit: str | None = None,
) -> dict[str, Any]:
    """Write the project to `out_id` (an mp4 in `media`). `progress` gets 0.0..1.0. Returns what was used:
    `{"preset", "fit", "width", "height", "fps"}`.

    The picture is the first clip's size unless `width` and `height` (see `check_size`) or an export `preset`
    (see `presets.py`, which also sets frame rate and encoder) say otherwise; explicit size beats the preset's.
    Every clip then fits that frame: `fit="letterbox"` (default) scales it to fit and pads, `"crop"` scales it to
    cover and cuts the overflow (clips with their own crop are not affected).

    With a `cancel` token (a job's `JobContext`), cancelling terminates ffmpeg at once, deletes
    the partial output and raises `JobCancelledError`.

    Clips are found by asset id (relinked if their file moved); a clip that can't be found
    is a `SourceNotFoundError` naming the asset and its last known path."""
    settings = presets.resolve_export(preset, width, height, fit)
    size = settings.size
    project = assets.resolve_project(media, project)  # every clip's `source` is now its file's current media id
    video_clips = [c for t in project.tracks if t.kind == "video" for c in t.clips]
    if not video_clips:
        raise TimelineError("the timeline has no clips to render")

    infos: dict[str, MediaInfo] = {}
    for track in project.tracks:
        for clip in track.clips:
            if clip.kind == "text":
                continue
            if clip.source not in infos:
                infos[clip.source] = media.probe(clip.source)
            if track.kind == "video" and infos[clip.source].width is None:
                raise TimelineError(f"{clip.source} has no video stream")
            if track.kind == "audio" and not infos[clip.source].has_audio:
                raise TimelineError(f"{clip.source} has no audio stream")

    main = _main_track(project)
    first = main.clips[0] if main else next((c for c in video_clips if c.kind == "media"), None)
    if size is not None:
        width, height = size
    elif first is None:
        width, height = DEFAULT_SIZE
    else:
        width, height = infos[first.source].width // 2 * 2, infos[first.source].height // 2 * 2
    font = find_font() if any(c.kind == "text" for c in video_clips) else None
    plan = build_plan(project, infos, width, height, font, settings.fps, settings.fit)
    total = project.duration

    try:
        with ExitStack() as stack:
            # Every input, and the output, as real paths for ffmpeg for the whole run.
            inputs = [str(stack.enter_context(media.local_path(c.source))) for c in plan.clips]
            out_path = stack.enter_context(media.write_local(out_id))
            _run_ffmpeg(plan, inputs, str(out_path), total, progress, cancel, settings.preset)
    except JobCancelledError:
        media.delete(out_id)  # never leave a half-written file behind
        raise
    return {
        "preset": settings.preset.name if settings.preset else None,
        "fit": settings.fit,
        "width": width,
        "height": height,
        "fps": settings.fps,
    }


def _encode_args(preset: presets.ExportPreset | None) -> list[str]:
    """The ffmpeg output options. Without a preset these are what renders always used."""
    if preset is None:
        return ["-c:v", "libx264", "-preset", presets.DEFAULT_X264_PRESET, "-c:a", "aac", "-movflags", "+faststart"]
    return [
        "-c:v", preset.video_codec, "-preset", preset.x264_preset, "-crf", str(preset.crf), "-pix_fmt", preset.pix_fmt,
        "-c:a", preset.audio_codec, "-b:a", preset.audio_bitrate, "-ar", str(preset.audio_sample_rate),
        *(["-movflags", "+faststart"] if preset.faststart else []),
    ]  # fmt: skip


def _run_ffmpeg(
    plan: Plan,
    inputs: list[str],
    out_path: str,
    total: float,
    progress: Callable[[float], None] | None,
    cancel: CancelToken | None = None,
    preset: presets.ExportPreset | None = None,
    stall_timeout: float | None = None,
) -> None:
    """Run the render. A cancelled job stops it; so does ffmpeg's `-progress` output standing still for
    `stall_timeout` seconds (default `ffmpeg.stall_seconds()`), which raises `ExtractError`."""
    cmd = [
        _require_binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-nostats",
        "-progress",
        "pipe:1",
        *_clip_inputs(plan.clips, inputs),
        "-filter_complex",
        plan.graph,
        "-map",
        f"[{plan.video_label}]",
        "-map",
        f"[{plan.audio_label}]",
        "-t",
        f"{total:.3f}",
        *_encode_args(preset),
        out_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stderr is not None
    # stderr is drained on a thread: reading it only after stdout ends deadlocks once ffmpeg has written
    # more than a pipe buffer of log (it blocks on stderr, so stdout never ends).
    tail: list[str] = []

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line)
            del tail[:-200]

    reader = threading.Thread(target=drain, name="ffmpeg-stderr", daemon=True)
    reader.start()
    detach = stop_on_cancel(proc, cancel)  # cancelling terminates ffmpeg, which ends the loop below
    stall = stall_seconds() if stall_timeout is None else stall_timeout
    try:
        with StallWatchdog(proc, stall) as watchdog:
            last_time = None
            for line in proc.stdout:
                if not line.startswith("out_time_us="):
                    continue
                value = line.split("=")[1].strip()
                if value != last_time:  # the watchdog counts only output time that moved
                    last_time = value
                    watchdog.touch()
                if progress:
                    try:
                        progress(min(1.0, max(0.0, int(value) / 1_000_000 / total)))
                    except ValueError:
                        pass  # ffmpeg prints out_time_us=N/A before the first frame
            returncode = proc.wait()
            reader.join(5)
    finally:
        detach()
        if proc.poll() is None:  # leaving early (an exception in `progress`): don't leak the process
            proc.kill()
            proc.wait()
    stderr = "".join(tail)
    if watchdog.stalled:
        raise ExtractError(
            f"ffmpeg stopped making progress rendering '{os.path.basename(out_path)}' for {stall:g} s and was killed: "
            f"{stderr.strip()[-500:]}"
        )
    if cancel is not None:
        cancel.raise_if_cancelled()
    if returncode != 0:
        raise ExtractError(f"ffmpeg failed rendering '{os.path.basename(out_path)}': {stderr.strip()[-2000:]}")
    if progress:
        progress(1.0)
