"""`split-video ocr`, `ocr-text` and `ocr-search`: the text on a screen recording's screen (see `ocr.py`).

`ocr` takes a video file under `--root` (default: the current directory) or an asset id (`a_...`, from `library list`),
reads the screen at its scene changes (if `scenes` has run) and every `--interval` seconds with Tesseract, and stores
the text next to the media; current text made with the same parameters is reused. Without the `tesseract` program it
prints the install hint and exits with code 4. `--json` prints the counts (`ocr.recognize`, the same JSON as the API
job result and the MCP tool). `ocr-text` prints stored text, optionally one span and one page of it, and
`ocr-search` finds text on screen (`ocr.ocr_search`); neither runs the engine.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import ocr
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.jobs import JobCancelledError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError
from split_video.timefmt import format_timestamp
from split_video.timeline import TimelineError
from split_video.transcribe_cli import _asset_id, _Json, _Root, _Target

console = Console()
error_console = Console(stderr=True)

UNAVAILABLE_EXIT_CODE = 4

_EXPECTED = (
    ocr.OcrUnavailableError,
    ocr.OcrError,
    ocr.OcrNotFoundError,
    ocr.OcrArgumentError,
    TimelineError,
    MediaError,
    ExtractError,
    ProbeError,
    FfmpegNotFoundError,
)


def _fail(exc: Exception) -> typer.Exit:
    error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
    return typer.Exit(code=UNAVAILABLE_EXIT_CODE if isinstance(exc, ocr.OcrUnavailableError) else 1)


def ocr_cmd(
    target: str = _Target,
    root: Path = _Root,
    interval: float = typer.Option(
        ocr.DEFAULT_INTERVAL, "--interval", min=ocr.MIN_INTERVAL, help="Read a frame every this many seconds."
    ),
    max_frames: int = typer.Option(
        ocr.DEFAULT_MAX_FRAMES, "--max-frames", min=1, max=ocr.MAX_FRAMES_LIMIT, help="Read at most this many frames."
    ),
    force: bool = typer.Option(False, "--force", help="Redo it even if current text made with these settings exists."),
    as_json: bool = _Json,
) -> None:
    """Read the text on TARGET's screen (scene changes plus every --interval seconds) and store it next to it."""
    media = LocalMediaStore(root.resolve())
    try:
        asset_id = _asset_id(media, target)
        ocr.require_available()  # before any frame is decoded
        with error_console.status(f"Reading the screen of {target}...") as status:

            def on_progress(done: int, total: int) -> None:
                status.update(f"Reading the screen of {target}: frame {done} of {total}...")

            result = ocr.recognize(
                media, asset_id, interval=interval, max_frames=max_frames, force=force, on_progress=on_progress
            )
    except _EXPECTED as exc:
        raise _fail(exc) from None
    except JobCancelledError:
        raise typer.Exit(code=130) from None
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    if "skipped" in result:
        console.print(f"Skipped {result['asset']}: {result['skipped']} (no picture to read).")
        return
    verb = "Read" if result["made"] else "Already read"
    console.print(
        f"{verb} {result['asset']}: {result['ocr_frames']} frame(s) read, {result['skipped_unchanged']} unchanged "
        f"skipped, {result['scene_frames']} from scene changes, over "
        f"{format_timestamp(result['duration'], result['duration'])} ({result['engine']})."
    )
    if result.get("note"):
        console.print(f"[yellow]Note:[/yellow] {escape(result['note'])}")
    console.print(f'Search it with `split-video ocr-search {result["asset"]} "<text>" --root {root}`.')


def ocr_text_cmd(
    target: str = _Target,
    root: Path = _Root,
    start: float | None = typer.Option(None, "--start", help="Only text up at or after this second."),
    end: float | None = typer.Option(None, "--end", help="Only text up before this second."),
    limit: int | None = typer.Option(None, "--limit", help="States per page (default 20, max 200)."),
    cursor: str | None = typer.Option(None, "--cursor", help="Continue after the previous page (its next_cursor)."),
    as_json: bool = _Json,
) -> None:
    """Print TARGET's stored on-screen text (one page, optionally one span). Never runs the engine."""
    media = LocalMediaStore(root.resolve())
    try:
        page = ocr.ocr_range(media, _asset_id(media, target), start, end, limit=limit, cursor=cursor)
    except _EXPECTED as exc:
        raise _fail(exc) from None
    if as_json:
        typer.echo(json.dumps(page, indent=2))
        return
    for item in page["items"]:
        stamp = format_timestamp(item["time"], page["duration"])
        console.print(f"[dim]{stamp}[/dim]", highlight=False, soft_wrap=True)
        console.print(escape(item["text"]), highlight=False, soft_wrap=True)
    console.print(f"[dim]{page['count']} of {page['total_in_range']} in range[/dim]")
    if page["next_cursor"]:
        console.print(f"More: add --cursor {page['next_cursor']}")


def ocr_search_cmd(
    target: str = _Target,
    query: str = typer.Argument(..., help="The text to find (fuzzy, case-insensitive, matched by words)."),
    root: Path = _Root,
    start: float | None = typer.Option(None, "--start", help="Only text up at or after this second."),
    end: float | None = typer.Option(None, "--end", help="Only text up before this second."),
    limit: int | None = typer.Option(None, "--limit", help="Hits per page (default 20, max 200)."),
    cursor: str | None = typer.Option(None, "--cursor", help="Continue after the previous page (its next_cursor)."),
    context: int | None = typer.Option(None, "--context", help="Lines of context each side (default 1, max 10)."),
    as_json: bool = _Json,
) -> None:
    """Find where QUERY was on TARGET's screen, with the time and the line it matched."""
    media = LocalMediaStore(root.resolve())
    try:
        found = ocr.ocr_search(
            media, _asset_id(media, target), query, start=start, end=end, limit=limit, cursor=cursor, context=context
        )
    except _EXPECTED as exc:
        raise _fail(exc) from None
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    duration = max([h["end"] for h in found["hits"]], default=0.0)
    for hit in found["hits"]:
        stamp = format_timestamp(hit["time"], duration)
        console.print(
            f"[dim]{stamp}[/dim]  {escape(hit['text'])}  [dim]score {hit['score']:.2f}[/dim]",
            highlight=False,
            soft_wrap=True,
        )
    console.print(f"[dim]{found['count']} of {found['total_matches']} matches[/dim]")
    if found["next_cursor"]:
        console.print(f"More: add --cursor {found['next_cursor']}")


def register(app: typer.Typer) -> None:
    app.command("ocr")(ocr_cmd)
    app.command("ocr-text")(ocr_text_cmd)
    app.command("ocr-search")(ocr_search_cmd)
