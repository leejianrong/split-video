"""Render a timeline `Project` to a single video file with ffmpeg.

The picture is built bottom-up on a black canvas as long as the project:

1. The main track (the first video track, when it is magnetic) is normalised
   to the first clip's size, frame rate and audio format and joined pairwise
   with `concat` for cuts or `xfade`/`acrossfade` for crossfades.
2. Every clip on any other video track is overlaid at its own start time with
   its own position, size and opacity, in track order (later tracks on top).
3. Clips with a colour adjustment get ffmpeg's `eq` filter (brightness,
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
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass

from split_video import assets
from split_video.ffmpeg import ExtractError, MediaInfo, _require_binary
from split_video.media_store import MediaStore
from split_video.timeline import Clip, Project, TimelineError, Track

FPS = 30
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


def _gain(clip: Clip) -> str:
    return "0" if clip.muted else f"{clip.gain:g}"


def _main_chain(clips: list[Clip], first_input: int, infos: dict[str, MediaInfo], width: int, height: int):
    """Filter parts joining the main track's clips; returns (parts, vlabel, alabel)."""
    parts: list[str] = []
    for n, clip in enumerate(clips):
        i = first_input + n
        parts.append(
            f"[{i}:v]setpts=PTS-STARTPTS,fps={FPS},"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease{_eq(clip)},"
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
    for n in range(1, len(clips)):
        clip = clips[n]
        nv, na = f"vj{n}", f"aj{n}"
        if clip.transition == "crossfade" and clip.transition_duration > 0:
            d = clip.transition_duration
            parts.append(f"[{vlabel}][v{n}]xfade=transition=fade:duration={d:.3f}:offset={length - d:.3f}[{nv}]")
            parts.append(f"[{alabel}][a{n}]acrossfade=d={d:.3f}[{na}]")
            length += clip.duration - d
        else:
            parts.append(f"[{vlabel}][v{n}]concat=n=2:v=1:a=0[{nv}]")
            parts.append(f"[{alabel}][a{n}]concat=n=2:v=0:a=1[{na}]")
            length += clip.duration
        vlabel, alabel = nv, na
    return parts, vlabel, alabel


def build_plan(project: Project, infos: dict[str, MediaInfo], width: int, height: int, font: str | None = None) -> Plan:
    main = _main_track(project)
    main_clips = list(main.clips) if main else []
    layers = _overlay_clips(project, main)  # picture clips and text, in stacking order
    overlays = [c for c in layers if c.kind == "media"]  # the ones that need an ffmpeg input
    audio_clips = _audio_clips(project)
    total = project.duration
    ordered = [*main_clips, *overlays, *audio_clips]

    parts: list[str] = []
    if main_clips:
        chain, vmain, alabel = _main_chain(main_clips, 0, infos, width, height)
        parts += chain
    else:
        alabel = "asilent"
        parts.append(f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS[{alabel}]")

    parts.append(f"color=c=black:s={width}x{height}:r={FPS}:d={total:.3f},format=yuv420p[canvas]")
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
        parts.append(
            f"[{i}:v]setpts=PTS-STARTPTS+{clip.start:.3f}/TB,fps={FPS},scale={w}:-2{_eq(clip)},"
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

    return Plan(ordered, ";".join(parts), vlabel, alabel, width, height)


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
) -> None:
    """Write the project to `out_id` (an mp4 in `media`). `progress` gets 0.0..1.0.

    Clips are found by asset id (relinked if their file moved); a clip that can't be found
    is a `SourceNotFoundError` naming the asset and its last known path."""
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
    if first is None:
        width, height = DEFAULT_SIZE
    else:
        width, height = infos[first.source].width // 2 * 2, infos[first.source].height // 2 * 2
    font = find_font() if any(c.kind == "text" for c in video_clips) else None
    plan = build_plan(project, infos, width, height, font)
    total = project.duration

    with ExitStack() as stack:
        # Every input, and the output, as real paths for ffmpeg for the whole run.
        inputs = [str(stack.enter_context(media.local_path(c.source))) for c in plan.clips]
        out_path = stack.enter_context(media.write_local(out_id))
        _run_ffmpeg(plan, inputs, str(out_path), total, progress)


def _run_ffmpeg(
    plan: Plan, inputs: list[str], out_path: str, total: float, progress: Callable[[float], None] | None
) -> None:
    cmd = [
        _require_binary("ffmpeg"),
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
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        out_path,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stderr is not None
    for line in proc.stdout:
        if progress and line.startswith("out_time_us="):
            try:
                progress(min(1.0, max(0.0, int(line.split("=")[1]) / 1_000_000 / total)))
            except ValueError:
                pass  # ffmpeg prints out_time_us=N/A before the first frame
    stderr = proc.stderr.read()
    if proc.wait() != 0:
        raise ExtractError(f"ffmpeg failed rendering '{os.path.basename(out_path)}': {stderr.strip()[-2000:]}")
    if progress:
        progress(1.0)
