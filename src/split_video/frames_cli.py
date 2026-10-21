"""`split-video frame`: one picture of a video at a time (see `frames.py`).

Takes a file under `--root` (default: the current directory) or an asset id (`a_...`, from `library list`) and a
time (seconds, `mm:ss` or `hh:mm:ss`) and writes a JPEG or PNG to `--out`. Without `--out` the file is
`<name>-<time>s.<ext>` in the current directory (never in the workspace's cache). The format follows `--out`'s
suffix (`.png`), else `--format`, else JPEG. `--json` prints what the picture is (time, size, source) instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import assets, cli_errors, frames
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError
from split_video.timefmt import parse_time
from split_video.timeline import TimelineError
from split_video.transcribe_cli import _asset_id

console = Console()
error_console = Console(stderr=True)

_EXPECTED = (frames.FrameError, TimelineError, MediaError, ExtractError, ProbeError, FfmpegNotFoundError)


def _time(text: str) -> float:
    try:
        return parse_time(text)
    except ValueError as exc:
        raise cli_errors.fail(exc, exit_code=2) from None


def frame_cmd(
    target: str = typer.Argument(..., help="A video file under --root, or an asset id (a_..., see `library list`)."),
    at: str = typer.Argument(..., metavar="TIME", help="Seconds, mm:ss or hh:mm:ss."),
    root: Path = typer.Option(
        Path("."), "--root", exists=True, file_okay=False, help="The workspace folder (the media root)."
    ),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Where to write the image (default: ./<name>-<time>s.jpg)."
    ),
    width: int | None = typer.Option(None, "--width", min=1, max=frames.MAX_DIMENSION, help="Scale to this width."),
    height: int | None = typer.Option(None, "--height", min=1, max=frames.MAX_DIMENSION, help="Scale to this height."),
    fmt: str | None = typer.Option(None, "--format", help="jpeg or png (default: from --out's suffix, else jpeg)."),
    quality: int = typer.Option(frames.DEFAULT_QUALITY, "--quality", min=1, max=100, help="JPEG quality."),
    source: str = typer.Option("auto", "--source", help="auto (the proxy if there is one) or original."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Write TARGET's picture at TIME to a JPEG or PNG file."""
    seconds = _time(at)
    media = LocalMediaStore(root.resolve())
    try:
        asset_id = _asset_id(media, target)
        if fmt is None:
            fmt = "png" if out is not None and out.suffix.lower() == ".png" else "jpeg"
        frame = frames.extract_frame(
            media, asset_id, seconds, width=width, height=height, format=fmt, quality=quality, source=source
        )
        if out is None:
            stem = Path(assets.get_asset(media, asset_id).path).stem
            out = Path(f"{stem}-{seconds:g}s.{'png' if frame.format == 'png' else 'jpg'}")
    except _EXPECTED as exc:
        raise cli_errors.fail(exc) from None
    out.write_bytes(frame.data)
    if as_json:
        typer.echo(json.dumps({**frame.info(), "out": str(out)}, indent=2))
        return
    console.print(
        f"Wrote {escape(str(out))}: {frame.width}x{frame.height} {frame.format} at {frame.time:g} s "
        f"from the {frame.source}.",
        highlight=False,
    )
