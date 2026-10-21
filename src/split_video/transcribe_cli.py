"""`split-video transcribe` and `split-video transcript`: speech to text with word timestamps (see `transcribe.py`).

`transcribe` takes a file under `--root` (default: the current directory) or an asset id (`a_...`, from
`library list`), makes the transcript (the model is downloaded into `<root>/.split-video-models/` on first use) and
stores it next to the media. Without the optional faster-whisper package it prints the install hint and exits with
code 4. `transcript` prints a stored transcript, or one span of it, and never runs the model; with `--limit`, `--cursor` or
`--words` it prints one page (`transcript_search.transcript_range`, the same JSON as the API and MCP) and
`transcript-search` finds a phrase (`transcript_search.transcript_search`).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import assets, cli_errors, transcribe, transcript_search
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.jobs import JobCancelledError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError, MediaStore
from split_video.timefmt import format_timestamp
from split_video.timeline import TimelineError

console = Console()
error_console = Console(stderr=True)

UNAVAILABLE_EXIT_CODE = 4

_Target = typer.Argument(..., help="A media file under --root, or an asset id (a_..., see `library list`).")
_Root = typer.Option(Path("."), "--root", exists=True, file_okay=False, help="The workspace folder (the media root).")
_Json = typer.Option(False, "--json", help="Print machine-readable JSON.")


def _asset_id(media: MediaStore, target: str) -> str:
    """The asset id for a path under the root (registering the file in the library if need be) or an id."""
    if assets.is_asset_id(target):
        return target
    try:
        media_id = media.resolve_user_path(target)
    except MediaError:
        raise typer.BadParameter(f"{target} is outside --root") from None
    if not media.is_file(media_id):
        raise typer.BadParameter(f"not a file under --root: {target}")
    return assets.register_path(media, media_id).id


def _fail(exc: Exception) -> typer.Exit:
    return cli_errors.fail(exc)


_EXPECTED = (
    transcribe.TranscriptionUnavailableError,
    transcribe.TranscriptionError,
    TimelineError,
    transcript_search.TranscriptNotFoundError,
    transcript_search.TranscriptQueryError,
    MediaError,
    ExtractError,
    ProbeError,
    FfmpegNotFoundError,
)


def transcribe_cmd(
    target: str = _Target,
    root: Path = _Root,
    model: str = typer.Option(
        transcribe.DEFAULT_MODEL, "--model", help="A faster-whisper model: tiny.en, base.en, small.en, base, small, ..."
    ),
    language: str | None = typer.Option(None, "--language", help="Language code, e.g. en. Default: detected."),
    force: bool = typer.Option(False, "--force", help="Redo it even if a current transcript from this model exists."),
    as_json: bool = _Json,
) -> None:
    """Transcribe TARGET's speech with word timestamps and store the transcript next to it."""
    media = LocalMediaStore(root.resolve())
    try:
        asset_id = _asset_id(media, target)
        transcribe.require_available()  # before the model or the audio is touched
        with error_console.status(f"Transcribing {target} with {model}..."):
            result = transcribe.transcribe(media, asset_id, model=model, language=language, force=force)
    except _EXPECTED as exc:
        raise _fail(exc) from None
    except JobCancelledError:
        raise typer.Exit(code=130) from None
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    verb = "Transcribed" if result["made"] else "Already transcribed"
    console.print(
        f"{verb} {result['asset']}: {result['words']} words in {result['segments']} segments, "
        f"{result['language']}, {format_timestamp(result['duration'], result['duration'])} of audio ({result['model']})."
    )
    console.print(f"Read it with `split-video transcript {result['asset']} --root {root}`.")


def transcript_cmd(
    target: str = _Target,
    root: Path = _Root,
    start: float | None = typer.Option(None, "--start", help="Only words from this second."),
    end: float | None = typer.Option(None, "--end", help="Only words up to this second."),
    limit: int | None = typer.Option(None, "--limit", help="Print one page of at most this many items (max 500)."),
    cursor: str | None = typer.Option(None, "--cursor", help="Continue after the previous page (its next_cursor)."),
    words: bool = typer.Option(False, "--words", help="Page by word instead of by segment."),
    max_chars: int | None = typer.Option(None, "--max-chars", help="Stop a page before it exceeds this much text."),
    as_json: bool = _Json,
) -> None:
    """Print TARGET's stored transcript (or the words between --start and --end). Never runs the model.

    With --limit, --cursor, --words or --max-chars it prints one page and the cursor for the next."""
    media = LocalMediaStore(root.resolve())
    paged = limit is not None or cursor is not None or words or max_chars is not None
    try:
        asset_id = _asset_id(media, target)
        if paged:
            page = transcript_search.transcript_range(
                media,
                asset_id,
                start,
                end,
                limit=limit,
                cursor=cursor,
                granularity="word" if words else "segment",
                max_chars=max_chars,
            )
        else:
            found = transcribe.load(media, asset_id)
            if found is None:
                raise transcript_search.TranscriptNotFoundError(
                    f"no transcript for {target}: run `split-video transcribe {target}` first."
                )
    except _EXPECTED as exc:
        raise _fail(exc) from None
    if paged:
        if as_json:
            typer.echo(json.dumps(page, indent=2))
            return
        for item in page["items"]:
            stamp = format_timestamp(item["start"], page["duration"])
            console.print(f"[dim]{stamp}[/dim]  {escape(item.get('text') or item['word'])}", highlight=False)
        console.print(f"[dim]{page['count']} of {page['total_in_range']} in range[/dim]")
        if page["next_cursor"]:
            console.print(f"More: add --cursor {page['next_cursor']}")
        return
    cut = transcribe.excerpt(found, start, end)
    if as_json:
        typer.echo(json.dumps(cut, indent=2))
        return
    for s in cut["segments"]:
        console.print(f"[dim]{format_timestamp(s['start'], found['duration'])}[/dim]  {s['text']}", highlight=False)


def transcript_search_cmd(
    target: str = _Target,
    query: str = typer.Argument(..., help="The word or phrase to find (case-insensitive, whole words)."),
    root: Path = _Root,
    start: float | None = typer.Option(None, "--start", help="Only hits from this second."),
    end: float | None = typer.Option(None, "--end", help="Only hits before this second."),
    limit: int | None = typer.Option(None, "--limit", help="Hits per page (default 20, max 200)."),
    cursor: str | None = typer.Option(None, "--cursor", help="Continue after the previous page (its next_cursor)."),
    context: int | None = typer.Option(None, "--context", help="Words of context each side (default 8, max 50)."),
    case_sensitive: bool = typer.Option(False, "--case-sensitive", help="Match case."),
    as_json: bool = _Json,
) -> None:
    """Find where QUERY is said in TARGET's transcript, with the time and the words around it."""
    media = LocalMediaStore(root.resolve())
    try:
        found = transcript_search.transcript_search(
            media,
            _asset_id(media, target),
            query,
            start=start,
            end=end,
            limit=limit,
            cursor=cursor,
            context=context,
            case_sensitive=case_sensitive,
        )
    except _EXPECTED as exc:
        raise _fail(exc) from None
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    duration = max([h["end"] for h in found["hits"]], default=0.0)
    for hit in found["hits"]:
        stamp = format_timestamp(hit["time"], duration)
        before, after = escape(hit["context_before"]), escape(hit["context_after"])
        console.print(f"[dim]{stamp}[/dim]  {before} [bold]{escape(hit['text'])}[/bold] {after}", highlight=False)
    console.print(f"[dim]{found['count']} of {found['total_matches']} matches[/dim]")
    if found["next_cursor"]:
        console.print(f"More: add --cursor {found['next_cursor']}")


def register(app: typer.Typer) -> None:
    app.command("transcribe")(transcribe_cmd)
    app.command("transcript")(transcript_cmd)
    app.command("transcript-search")(transcript_search_cmd)
