"""`split-video at` and `timeline-index`: what is happening at a time of a recording (see `time_index.py`).

`at TARGET TIME` prints every stored signal around TIME (seconds or `1:23:45`) in a `--window` (default 10 s): audio
class, loudness, words spoken, scene cuts and on-screen text. `timeline-index TARGET` prints compact points over a span
(`--start` / `--end` / `--step`). Both take a video file under `--root` or an asset id, only read what other commands
stored (a missing signal is reported with the command that makes it), and with `--json` print the same JSON as the
API and the MCP tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import time_index
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError
from split_video.timefmt import format_timestamp, parse_time
from split_video.timeline import TimelineError
from split_video.transcribe_cli import _asset_id, _Json, _Root, _Target

console = Console()
error_console = Console(stderr=True)

_EXPECTED = (time_index.TimeIndexError, TimelineError, MediaError)


def _fail(exc: Exception) -> typer.Exit:
    error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
    return typer.Exit(code=1)


def _seconds(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return parse_time(text)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def _say(text: str) -> None:
    console.print(text, highlight=False, soft_wrap=True)


def at_cmd(
    target: str = _Target,
    time: str = typer.Argument(..., help="Seconds (83.5) or a clock time (1:23.5, 1:02:03)."),
    root: Path = _Root,
    window: float | None = typer.Option(
        None,
        "--window",
        help=f"Seconds around TIME (default {time_index.DEFAULT_WINDOW:g}, max {time_index.MAX_WINDOW:g}).",
    ),
    as_json: bool = _Json,
) -> None:
    """Show what is happening in TARGET around TIME: audio, loudness, speech, scene and on-screen text."""
    media = LocalMediaStore(root.resolve())
    seconds = _seconds(time)
    try:
        found = time_index.what_is_happening(media, _asset_id(media, target), seconds, window)
    except _EXPECTED as exc:
        raise _fail(exc) from None
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    span = found["window"]
    total = found["duration"]
    _say(
        f"[dim]{format_timestamp(found['time'], total)}  window "
        f"{format_timestamp(span['start'], total)} to {format_timestamp(span['end'], total)}[/dim]"
    )
    if found["audio"]:
        audio = found["audio"]
        also = f", then {audio['secondary']}" if audio["secondary"] else ""
        _say(f"audio: {audio['class']}{also}")
    if found["loudness"]:
        loud = found["loudness"]
        _say(f"loudness: mean {loud['mean_db']} dB, max {loud['max_db']} dB")
    if found["silence"]:
        _say(f"silence: {'inside a silence' if found['silence']['inside'] else 'not silent'}")
    if found["speech"]:
        speech = found["speech"]
        _say(f"speech: {speech['word_count']} words, {speech['words_per_minute']:g}/min: {escape(speech['text'])}")
    visual = found["visual"]
    if visual["scene_index"] is not None:
        cut = visual["scene_change"]
        near = f", cut at {cut['time']:g} (score {cut['score']:g})" if cut else ""
        _say(f"scene: shot {visual['scene_index']} from {visual['shot_start']:g}{near}")
    if visual["text"]:
        _say(f"on screen: {escape(visual['text']['text'].splitlines()[0] if visual['text']['text'] else '')}")
    for item in found["missing"]:
        _say(f"[yellow]{item['signal']}:[/yellow] {escape(item['how'])}")


def timeline_index_cmd(
    target: str = _Target,
    root: Path = _Root,
    start: str | None = typer.Option(None, "--start", help="Where to begin (default 0)."),
    end: str | None = typer.Option(None, "--end", help="Where to stop (default the end)."),
    step: float | None = typer.Option(
        None,
        "--step",
        help=f"Seconds per point (default {time_index.DEFAULT_STEP:g}, at most {time_index.MAX_POINTS} points).",
    ),
    as_json: bool = _Json,
) -> None:
    """Show compact points over TARGET: audio class, loudness, words, cuts and on-screen text per step."""
    media = LocalMediaStore(root.resolve())
    lo, hi = _seconds(start), _seconds(end)
    try:
        found = time_index.timeline_index(media, _asset_id(media, target), lo, hi, step)
    except _EXPECTED as exc:
        raise _fail(exc) from None
    if as_json:
        typer.echo(json.dumps(found, indent=2))
        return
    total = found["duration"]
    for p in found["points"]:
        words = "" if p["word_count"] is None else f"{p['word_count']} words"
        loud = "" if p["loudness_db"] is None else f"{p['loudness_db']} dB"
        text = "" if not p["text"] else escape(p["text"])
        _say(f"[dim]{format_timestamp(p['start'], total)}[/dim]  {p['audio'] or '-'}  {loud}  {words}  {text}")
    for item in found["missing"]:
        _say(f"[yellow]{item['signal']}:[/yellow] {escape(item['how'])}")


def register(app: typer.Typer) -> None:
    app.command("at")(at_cmd)
    app.command("timeline-index")(timeline_index_cmd)
