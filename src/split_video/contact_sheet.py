"""One image of N frames from a stretch of an asset, each with its time burned in: a cheap way to skim (SPL-46).

`make_sheet(media, asset_id, start, end, count=12, ...)` returns the encoded grid (JPEG or PNG) plus the map from
each tile to its time and position, so an agent that spots something in tile 7 knows where (and when) it is. The
HTTP route, the CLI (`split-video contact-sheet`) and the MCP tool (`get_contact_sheet`) are thin adapters.
`plan_sheet` computes the same layout and times without touching ffmpeg (the `/info` variant).

Times
-----
The range `[start, end)` is cut into `count` equal sub-intervals and each tile shows the frame at the CENTRE of
its sub-interval: `time_i = start + (i + 0.5) * (end - start) / count`. Centres summarise the stretch they stand
for (a first-frame-of-interval scheme would show the very frame of `start` but never anything near `end`), and they
are never at the end of the file, so no tile needs the last-frame workaround in the usual case. `end` defaults to
the asset's duration; a range past the duration by at most `END_TOLERANCE` is clamped to it; anything further is a
`FrameTimeError` that states the duration. `start < end` is required.

Layout
------
Tiles run left to right, then top to bottom (row-major, tile 0 top left). `columns` defaults to
`ceil(sqrt(count))`, which favours wider than tall (12 -> 4x3, 10 -> 4x3, 16 -> 4x4, 6 -> 3x2); rows follow. A last
row that is not full is left black. Each tile is `tile_width` wide (default 320, 64..1280, rounded down to an
even number) and as tall as the picture's aspect ratio makes it; a picture is scaled to the tile, up or down. The
whole image is at most `MAX_SHEET_WIDTH` (2560) px wide and `count` is at most `MAX_COUNT` (64), so one sheet is
a few MB at most and a model can still read the tiles; ask for fewer columns or a narrower tile to fit.

Timestamps
----------
Each tile carries its sample time in its top-left corner: white text on a 60 % black box, `H:MM:SS` when the range
ends at or after one hour, else `M:SS`; when the tiles are less than 10 s apart tenths are added (`M:SS.s`,
`H:MM:SS.s`) because whole seconds would repeat. The font is `render.find_font()` (a clear error if none is
installed) at 5.5 % of the tile width (about 18 px at 320), never under 12 px.

Cost
----
The work is bounded by `count`, not by the length of the range or the file: each tile is one input-side seek
(`-ss` before `-i`, as `frames.py`), which decodes one GOP, a few of them in parallel; then one ffmpeg call tiles
the raw pictures. A frame that cannot be decoded (a seek into the last fraction of a file) is retried a little
earlier and, failing that, the previous tile's picture is repeated (its timestamp still says the requested time).
`source` is as in `frames.py`: "auto" reads the proxy if there is one (small, fast, 1/15 s accurate), "original" the file.
"""

from __future__ import annotations

import math
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from split_video import assets, proxy
from split_video.ffmpeg import ExtractError, _require_binary, run_cancellable, stop_on_cancel
from split_video.frames import (
    END_TOLERANCE,
    FORMATS,
    SOURCES,
    FrameArgumentError,
    FrameAudioOnlyError,
    FrameError,
    FrameTimeError,
    normalize_format,
)
from split_video.jobs import CancelToken
from split_video.media_store import MediaStore
from split_video.render import find_font
from split_video.timeline import TimelineError

MAX_COUNT = 64
DEFAULT_COUNT = 12
DEFAULT_TILE_WIDTH = 320
MIN_TILE_WIDTH = 64
MAX_TILE_WIDTH = 1280
MAX_SHEET_WIDTH = 2560
JPEG_QUALITY = 3  # ffmpeg -q:v (2 best .. 31 worst): sharp enough to read small digits
FONT_RATIO = 0.055
MIN_FONT = 12
TENTHS_BELOW = 10.0  # seconds between tiles under which timestamps show tenths
WORKERS = 4
_END_RETRIES = (0.1, 0.5, 1.5)

__all__ = ["FrameArgumentError", "FrameAudioOnlyError", "FrameError", "FrameTimeError", "make_sheet", "plan_sheet"]


class SheetFontError(FrameError):
    """No TrueType font is installed for the timestamps."""


@dataclass(frozen=True)
class Tile:
    index: int
    time: float  # seconds into the asset that the tile shows
    x: int  # top-left corner in the sheet, pixels
    y: int

    def to_dict(self) -> dict[str, object]:
        return {"index": self.index, "time": self.time, "x": self.x, "y": self.y}


@dataclass(frozen=True)
class Layout:
    asset: str
    start: float
    end: float
    duration: float  # of the asset
    count: int
    columns: int
    rows: int
    tile_width: int
    tile_height: int
    width: int  # of the whole image
    height: int
    tiles: tuple[Tile, ...]
    source: str  # "proxy" or "original": what is (or would be) read

    def info(self) -> dict[str, object]:
        """The tile map and geometry, no pixels (what the `/info` route, `--json` and the MCP text say)."""
        return {
            "asset": self.asset,
            "start": self.start,
            "end": self.end,
            "duration": self.duration,
            "count": self.count,
            "columns": self.columns,
            "rows": self.rows,
            "tile_width": self.tile_width,
            "tile_height": self.tile_height,
            "width": self.width,
            "height": self.height,
            "source": self.source,
            "tiles": [t.to_dict() for t in self.tiles],
        }


@dataclass(frozen=True)
class Sheet:
    data: bytes
    mime: str
    format: str
    layout: Layout

    def info(self) -> dict[str, object]:
        return {**self.layout.info(), "mime": self.mime, "format": self.format, "bytes": len(self.data)}


def auto_columns(count: int) -> int:
    return math.ceil(math.sqrt(count))


def format_time(seconds: float, *, hours: bool, tenths: bool) -> str:
    """`H:MM:SS` / `M:SS`, with `.t` appended when `tenths` (truncated, so a time never reads later than it is)."""
    scaled = int(seconds * 10 + 1e-6) if tenths else int(seconds + 1e-6) * 10
    whole, tenth = divmod(scaled, 10)
    h, rest = divmod(whole, 3600)
    m, s = divmod(rest, 60)
    text = f"{h}:{m:02d}:{s:02d}" if hours else f"{h * 60 + m}:{s:02d}"
    return f"{text}.{tenth}" if tenths else text


def plan_sheet(
    media: MediaStore,
    asset_id: str,
    start: float = 0.0,
    end: float | None = None,
    *,
    count: int = DEFAULT_COUNT,
    columns: int | None = None,
    tile_width: int = DEFAULT_TILE_WIDTH,
    source: str = "auto",
) -> Layout:
    """Validate the request and compute the layout and tile times (no ffmpeg). Raises as `make_sheet`."""
    if source not in SOURCES:
        raise FrameArgumentError(f"unknown source {source!r}: use auto or original")
    if not 1 <= count <= MAX_COUNT:
        raise FrameArgumentError(f"count must be between 1 and {MAX_COUNT}, not {count}")
    if not MIN_TILE_WIDTH <= tile_width <= MAX_TILE_WIDTH:
        raise FrameArgumentError(f"tile_width must be between {MIN_TILE_WIDTH} and {MAX_TILE_WIDTH} pixels")
    tile_width -= tile_width % 2
    if columns is None:
        columns = auto_columns(count)
    elif not 1 <= columns <= count:
        raise FrameArgumentError(f"columns must be between 1 and count ({count}), not {columns}")
    if columns * tile_width > MAX_SHEET_WIDTH:
        raise FrameArgumentError(
            f"{columns} columns of {tile_width} px make a {columns * tile_width} px wide image; "
            f"the limit is {MAX_SHEET_WIDTH}: use fewer columns or a smaller tile_width"
        )
    asset = assets.get_asset(media, asset_id)
    if not asset.has_video or not asset.width or not asset.height:
        raise FrameAudioOnlyError(f"asset {asset.id} is audio-only: it has no picture for a contact sheet")
    if math.isnan(start) or start < 0:
        raise FrameTimeError(f"start must be 0 or more seconds, not {start}")
    stop = asset.duration if end is None else end
    if math.isnan(stop) or stop > asset.duration + END_TOLERANCE:
        raise FrameTimeError(f"end {stop:g} s is past the end of the asset, which is {asset.duration:g} s long")
    stop = min(stop, asset.duration)
    if start > asset.duration + END_TOLERANCE:
        raise FrameTimeError(f"start {start:g} s is past the end of the asset, which is {asset.duration:g} s long")
    if start >= stop:
        raise FrameTimeError(f"start ({start:g} s) must be before end ({stop:g} s)")
    rows = math.ceil(count / columns)
    tile_height = round(tile_width * asset.height / asset.width / 2) * 2 or 2
    step = (stop - start) / count
    tiles = tuple(
        Tile(i, round(start + (i + 0.5) * step, 3), (i % columns) * tile_width, (i // columns) * tile_height)
        for i in range(count)
    )
    kind = (
        "original"
        if source == "original"
        else ("proxy" if proxy.visual_source(media, asset.id) != asset.path else "original")
    )
    return Layout(
        asset=asset.id,
        start=start,
        end=stop,
        duration=asset.duration,
        count=count,
        columns=columns,
        rows=rows,
        tile_width=tile_width,
        tile_height=tile_height,
        width=columns * tile_width,
        height=rows * tile_height,
        tiles=tiles,
        source=kind,
    )


def _quote(text: str) -> str:
    """`text` as one single-quoted drawtext option value. ffmpeg 4.4 still wants the colon escaped inside the
    quotes (a timestamp is full of them): `0\\:01\\:02`."""
    return "'" + text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''") + "'"


def _tile_command(ffmpeg: str, src: str, seek: float, label: str, layout: Layout, font: str) -> list[str]:
    size = max(MIN_FONT, round(layout.tile_width * FONT_RATIO))
    pad = max(3, size // 4)
    draw = (
        f"drawtext=fontfile={_quote(font)}:text={_quote(label)}:fontsize={size}:fontcolor=white"
        f":box=1:boxcolor=black@0.6:boxborderw={pad}:x={pad + 2}:y={pad + 2}"
    )
    vf = f"scale={layout.tile_width}:{layout.tile_height},setsar=1,{draw}"
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{seek:.3f}",
        "-i",
        src,
        "-an",
        "-sn",
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "-",
    ]


def make_sheet(
    media: MediaStore,
    asset_id: str,
    start: float = 0.0,
    end: float | None = None,
    *,
    count: int = DEFAULT_COUNT,
    columns: int | None = None,
    tile_width: int = DEFAULT_TILE_WIDTH,
    format: str = "jpeg",
    source: str = "auto",
    cancel: CancelToken | None = None,
) -> Sheet:
    """The contact sheet of `[start, end)` of the asset (see the module docstring).

    Raises `FrameArgumentError` (bad count, columns, tile_width, format, source, or an image wider than the cap),
    `FrameTimeError` (bad range: the message states the duration), `FrameAudioOnlyError`, `AssetNotFoundError` / `SourceNotFoundError` for a missing asset, `SheetFontError` if no font is installed,
    `ExtractError` if ffmpeg fails (or no tile at all could be decoded) and `JobCancelledError` through `cancel`."""
    fmt = normalize_format(format)
    layout = plan_sheet(media, asset_id, start, end, count=count, columns=columns, tile_width=tile_width, source=source)
    try:
        font = find_font()
    except TimelineError as exc:
        raise SheetFontError(str(exc)) from None
    ffmpeg = _require_binary("ffmpeg")
    asset = assets.get_asset(media, layout.asset)
    source_id = asset.path if layout.source == "original" else proxy.visual_source(media, asset.id)
    tenths = (layout.end - layout.start) / layout.count < TENTHS_BELOW
    hours = layout.end >= 3600
    frame_bytes = layout.tile_width * layout.tile_height * 3

    with media.local_path(source_id) as path:

        def grab(tile: Tile) -> bytes | None:
            label = format_time(tile.time, hours=hours, tenths=tenths)
            seeks = [tile.time]
            if tile.time >= asset.duration - _END_RETRIES[-1]:
                seeks += [max(0.0, tile.time - back) for back in _END_RETRIES]
            for seek in seeks:
                result = run_cancellable(
                    _tile_command(ffmpeg, str(path), seek, label, layout, font), cancel, text=False
                )
                if result.returncode == 0 and len(result.stdout) == frame_bytes:
                    return result.stdout
            return None

        with ThreadPoolExecutor(max_workers=min(WORKERS, layout.count)) as pool:
            grabbed = list(pool.map(grab, layout.tiles))
    if all(g is None for g in grabbed):
        raise ExtractError(f"could not extract any frame of {asset.path} between {layout.start:g} and {layout.end:g} s")
    # a tile that did not decode repeats the previous one (or, at the very start, the first that did)
    first = next(g for g in grabbed if g is not None)
    previous = first
    frames_raw = []
    for g in grabbed:
        previous = g if g is not None else previous
        frames_raw.append(previous)

    codec = ["-c:v", "mjpeg", "-q:v", str(JPEG_QUALITY)] if fmt == "jpeg" else ["-c:v", "png"]
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{layout.tile_width}x{layout.tile_height}",
        "-framerate",
        "1",
        "-i",
        "-",
        "-vf",
        f"tile={layout.columns}x{layout.rows}:padding=0:margin=0,format={'yuvj420p' if fmt == 'jpeg' else 'rgb24'}",
        "-frames:v",
        "1",
        *codec,
        "-f",
        "image2pipe",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    detach = stop_on_cancel(proc, cancel)
    try:
        stdout, stderr = proc.communicate(b"".join(frames_raw))
    finally:
        detach()
    if cancel is not None:
        cancel.raise_if_cancelled()
    if proc.returncode != 0 or not stdout:
        tail = stderr.decode(errors="replace").strip().splitlines()[-3:]
        raise ExtractError("could not assemble the contact sheet: " + " ".join(tail))
    return Sheet(data=stdout, mime=FORMATS[fmt], format=fmt, layout=layout)
