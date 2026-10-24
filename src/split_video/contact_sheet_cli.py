"""`split-video contact-sheet`: one image of N frames from a stretch of a video (see `contact_sheet.py`).

Takes a file under `--root` (default: the current directory) or an asset id (`a_...`, from `library list`) and
writes a JPEG or PNG grid, each tile stamped with its time, to `--out` (default `<name>-sheet-<start>s.jpg` in the
current directory, never in the workspace's cache; the format follows `--out`'s suffix, else `--format`). `--start`
and `--end` are seconds, `mm:ss` or `hh:mm:ss`. `--json` prints the tile map (time and pixel position of each
tile) instead of the one-line summary.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import assets, cli_errors, contact_sheet
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.frames_cli import _time
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError
from split_video.timeline import TimelineError
from split_video.transcribe_cli import _asset_id

console = Console()
error_console = Console(stderr=True)

_EXPECTED = (contact_sheet.FrameError, TimelineError, MediaError, ExtractError, ProbeError, FfmpegNotFoundError)


def contact_sheet_cmd(
    target: str = typer.Argument(..., help="A video file under --root, or an asset id (a_..., see `library list`)."),
    root: Path = typer.Option(
        Path("."), "--root", exists=True, file_okay=False, help="The workspace folder (the media root)."
    ),
    start: str = typer.Option("0", "--start", help="Start of the range: seconds, mm:ss or hh:mm:ss."),
    end: str | None = typer.Option(None, "--end", help="End of the range (default: the end of the video)."),
    count: int = typer.Option(
        contact_sheet.DEFAULT_COUNT, "--count", "-n", min=1, max=contact_sheet.MAX_COUNT, help="Number of frames."
    ),
    columns: int | None = typer.Option(None, "--columns", min=1, help="Tiles per row (default: near-square)."),
    tile_width: int = typer.Option(
        contact_sheet.DEFAULT_TILE_WIDTH,
        "--tile-width",
        min=contact_sheet.MIN_TILE_WIDTH,
        max=contact_sheet.MAX_TILE_WIDTH,
        help="Width of one tile in pixels.",
    ),
    out: Path | None = typer.Option(None, "--out", "-o", help="Where to write the image."),
    fmt: str | None = typer.Option(None, "--format", help="jpeg or png (default: from --out's suffix, else jpeg)."),
    source: str = typer.Option("auto", "--source", help="auto (the proxy if there is one) or original."),
    as_json: bool = typer.Option(False, "--json", help="Print the tile map as JSON."),
) -> None:
    """Write a grid of frames from TARGET, each with its time burned in, to one JPEG or PNG file."""
    first = _time(start)
    last = _time(end) if end is not None else None
    media = LocalMediaStore(root.resolve())
    try:
        asset_id = _asset_id(media, target)
        if fmt is None:
            fmt = "png" if out is not None and out.suffix.lower() == ".png" else "jpeg"
        sheet = contact_sheet.make_sheet(
            media, asset_id, first, last, count=count, columns=columns, tile_width=tile_width, format=fmt, source=source
        )
        if out is None:
            stem = Path(assets.get_asset(media, asset_id).path).stem
            out = Path(f"{stem}-sheet-{first:g}s.{'png' if sheet.format == 'png' else 'jpg'}")
    except _EXPECTED as exc:
        raise cli_errors.fail(exc) from None
    out.write_bytes(sheet.data)
    if as_json:
        typer.echo(json.dumps({**sheet.info(), "out": str(out)}, indent=2))
        return
    lay = sheet.layout
    console.print(
        f"Wrote {escape(str(out))}: {lay.width}x{lay.height} {sheet.format}, {lay.count} frames "
        f"({lay.columns}x{lay.rows} of {lay.tile_width}x{lay.tile_height}) from {lay.start:g} to {lay.end:g} s "
        f"of the {lay.source}.",
        highlight=False,
    )
