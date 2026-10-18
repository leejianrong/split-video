"""Render a timeline `Project` to a single video file with ffmpeg.

Phase 2 handles one gap-free video track: each clip is trimmed with
input-side `-ss/-t`, normalised to the first clip's size, frame rate and
audio format, then joined pairwise with `concat` for cuts or
`xfade`/`acrossfade` for crossfades.

`build_command` is pure (it only needs probed media info) so the graph can
be unit tested without ffmpeg; `render` probes, runs ffmpeg and reports
progress.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from split_video.ffmpeg import ExtractError, MediaInfo, _require_binary, probe_media
from split_video.timeline import Clip, Project, TimelineError, resolve_source

FPS = 30
AUDIO_RATE = 48000


def _clip_inputs(clips: list[Clip], root: Path) -> list[str]:
    args: list[str] = []
    for clip in clips:
        args += [
            "-ss",
            f"{clip.in_point:.3f}",
            "-t",
            f"{clip.duration:.3f}",
            "-i",
            str(resolve_source(root, clip.source)),
        ]
    return args


def build_filter_graph(clips: list[Clip], infos: list[MediaInfo], width: int, height: int) -> tuple[str, str, str]:
    """The `filter_complex` string and the final video and audio labels."""
    parts: list[str] = []
    for i, (clip, info) in enumerate(zip(clips, infos, strict=True)):
        parts.append(
            f"[{i}:v]setpts=PTS-STARTPTS,fps={FPS},"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v{i}]"
        )
        fmt = f"aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"
        if info.has_audio:
            parts.append(f"[{i}:a]asetpts=PTS-STARTPTS,{fmt},atrim=0:{clip.duration:.3f}[a{i}]")
        else:
            parts.append(f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=0:{clip.duration:.3f},asetpts=PTS-STARTPTS[a{i}]")

    vlabel, alabel = "v0", "a0"
    length = clips[0].duration
    for i in range(1, len(clips)):
        clip = clips[i]
        nv, na = f"vj{i}", f"aj{i}"
        if clip.transition == "crossfade" and clip.transition_duration > 0:
            d = clip.transition_duration
            parts.append(f"[{vlabel}][v{i}]xfade=transition=fade:duration={d:.3f}:offset={length - d:.3f}[{nv}]")
            parts.append(f"[{alabel}][a{i}]acrossfade=d={d:.3f}[{na}]")
            length += clip.duration - d
        else:
            parts.append(f"[{vlabel}][v{i}]concat=n=2:v=1:a=0[{nv}]")
            parts.append(f"[{alabel}][a{i}]concat=n=2:v=0:a=1[{na}]")
            length += clip.duration
        vlabel, alabel = nv, na
    return ";".join(parts), vlabel, alabel


def _video_clips(project: Project) -> list[Clip]:
    track = next((t for t in project.tracks if t.kind == "video"), None)
    if track is None or not track.clips:
        raise TimelineError("the timeline has no clips to render")
    return list(track.clips)


def render(
    project: Project,
    root: Path,
    out_path: Path,
    progress: Callable[[float], None] | None = None,
) -> None:
    """Write the project to `out_path` (an mp4). `progress` gets 0.0..1.0."""
    clips = _video_clips(project)
    infos: list[MediaInfo] = []
    for clip in clips:
        source = resolve_source(root, clip.source)
        if not source.is_file():
            raise TimelineError(f"source file not found: {clip.source}")
        info = probe_media(source)
        if info.width is None:
            raise TimelineError(f"{clip.source} has no video stream")
        infos.append(info)

    width, height = infos[0].width // 2 * 2, infos[0].height // 2 * 2
    graph, vlabel, alabel = build_filter_graph(clips, infos, width, height)
    total = project.duration

    cmd = [
        _require_binary("ffmpeg"),
        "-y",
        "-nostats",
        "-progress",
        "pipe:1",
        *_clip_inputs(clips, root),
        "-filter_complex",
        graph,
        "-map",
        f"[{vlabel}]",
        "-map",
        f"[{alabel}]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(out_path),
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
        raise ExtractError(f"ffmpeg failed rendering '{out_path.name}': {stderr.strip()[-2000:]}")
    if progress:
        progress(1.0)
