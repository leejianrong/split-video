"""`split-video search` and `visual-search`: find what was said or shown in a recording (see `search.py`).

`search` looks in the transcript and the on-screen text together and prints ranked moments (`--sources` limits it,
`--start` / `--end` / `--limit` / `--cursor` narrow and page it); `visual-search` looks at what the screen showed only
(the on-screen text search with a `sources` summary). Both take a video file under `--root` or an asset id, never run
a model, and with `--json` print the same JSON as the API and the MCP tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import ocr, search, transcript_search
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError
from split_video.timefmt import format_timestamp
from split_video.timeline import TimelineError
from split_video.transcribe_cli import _asset_id, _Json, _Root, _Target

console = Console()
error_console = Console(stderr=True)

_EXPECTED = (
    search.SearchNotFoundError,
    search.SearchQueryError,
    ocr.OcrNotFoundError,
    ocr.OcrArgumentError,
    transcript_search.TranscriptNotFoundError,
    transcript_search.TranscriptQueryError,
    TimelineError,
    MediaError,
    ExtractError,
    ProbeError,
    FfmpegNotFoundError,
)


def _fail(exc: Exception) -> typer.Exit:
    error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
    return typer.Exit(code=1)


def search_cmd(
    target: str = _Target,
    query: str = typer.Argument(..., help="What to find: said in the transcript or shown on screen."),
    root: Path = _Root,
    sources: str | None = typer.Option(None, "--sources", help="Where to look: transcript, ocr or transcript,ocr."),
    start: float | None = typer.Option(None, "--start", help="Only hits at or after this second."),
    end: float | None = typer.Option(None, "--end", help="Only hits before this second."),
    limit: int | None = typer.Option(None, "--limit", help="Hits per page (default 20, max 200)."),
    cursor: str | None = typer.Option(None, "--cursor", help="Continue after the previous page (its next_cursor)."),
    context: int | None = typer.Option(None, "--context", help="Words or lines of context each side (max 10)."),
    as_json: bool = _Json,
) -> None:
    """Find the moments QUERY was said or shown in TARGET, best first (transcript and on-screen text together)."""
    media = LocalMediaStore(root.resolve())
    try:
        found = search.search_asset(
            media,
            _asset_id(media, target),
            query,
            sources=sources,
            start=start,
            end=end,
            limit=limit,
            cursor=cursor,
            context=context,
        )
    except _EXPECTED as exc:
        raise _fail(exc) from None
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    duration = max([h["end"] for h in found["hits"]], default=0.0)
    for hit in found["hits"]:
        stamp = format_timestamp(hit["time"], duration)
        where = "+".join(hit["found_in"])
        console.print(
            f"[dim]{stamp}[/dim]  {escape(hit['text'])}  [dim]{where}, score {hit['score']:.2f}[/dim]",
            highlight=False,
            soft_wrap=True,
        )
    for name, status in found["sources"].items():
        if status != "searched" and status != "not requested":
            console.print(f"[yellow]{name}:[/yellow] {escape(status)}", highlight=False, soft_wrap=True)
    console.print(f"[dim]{found['count']} of {found['total_matches']} matches[/dim]")
    if found["next_cursor"]:
        console.print(f"More: add --cursor {found['next_cursor']}")


def visual_search_cmd(
    target: str = _Target,
    query: str = typer.Argument(..., help="What to find on screen (fuzzy, case-insensitive, matched by words)."),
    root: Path = _Root,
    start: float | None = typer.Option(None, "--start", help="Only text up at or after this second."),
    end: float | None = typer.Option(None, "--end", help="Only text up before this second."),
    limit: int | None = typer.Option(None, "--limit", help="Hits per page (default 20, max 200)."),
    cursor: str | None = typer.Option(None, "--cursor", help="Continue after the previous page (its next_cursor)."),
    context: int | None = typer.Option(None, "--context", help="Lines of context each side (default 1, max 10)."),
    as_json: bool = _Json,
) -> None:
    """Find where QUERY was on TARGET's screen (the on-screen text), best first."""
    media = LocalMediaStore(root.resolve())
    try:
        found = search.visual_search(
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
    app.command("search")(search_cmd)
    app.command("visual-search")(visual_search_cmd)
