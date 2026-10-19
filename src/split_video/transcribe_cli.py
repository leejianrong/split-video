"""`split-video transcribe` and `split-video transcript`: speech to text with word timestamps (see `transcribe.py`).

`transcribe` takes a file under `--root` (default: the current directory) or an asset id (`a_...`, from
`library list`), makes the transcript (the model is downloaded into `<root>/.split-video-models/` on first use) and
stores it next to the media. Without the optional faster-whisper package it prints the install hint and exits with
code 4. `transcript` prints a stored transcript, or one span of it, and never runs the model.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import assets, transcribe
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
    error_console.print(f"[red]Error:[/red] {escape(str(exc))}")  # the hint has `[transcribe]` in it
    return typer.Exit(code=UNAVAILABLE_EXIT_CODE if isinstance(exc, transcribe.TranscriptionUnavailableError) else 1)


_EXPECTED = (
    transcribe.TranscriptionUnavailableError,
    transcribe.TranscriptionError,
    TimelineError,
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
    as_json: bool = _Json,
) -> None:
    """Print TARGET's stored transcript (or the words between --start and --end). Never runs the model."""
    media = LocalMediaStore(root.resolve())
    try:
        found = transcribe.load(media, _asset_id(media, target))
    except _EXPECTED as exc:
        raise _fail(exc) from None
    if found is None:
        error_console.print(
            f"[red]Error:[/red] no transcript for {target}: run `split-video transcribe {target}` first."
        )
        raise typer.Exit(code=1)
    cut = transcribe.excerpt(found, start, end)
    if as_json:
        typer.echo(json.dumps(cut, indent=2))
        return
    for s in cut["segments"]:
        console.print(f"[dim]{format_timestamp(s['start'], found['duration'])}[/dim]  {s['text']}", highlight=False)


def register(app: typer.Typer) -> None:
    app.command("transcribe")(transcribe_cmd)
    app.command("transcript")(transcript_cmd)
