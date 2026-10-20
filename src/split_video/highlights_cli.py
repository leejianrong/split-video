"""`split-video highlights`: suggested highlight candidates of a recording (see `highlights.py`).

`highlights TARGET` scores windows from the stored speech, loudness, audio classes, scene changes, screen activity and
on-screen text, and prints a table of ranked, non-overlapping candidates (start, end, score, reasons); `--json` prints
the same JSON as the API and the MCP tool. `--preset` picks the weights for the kind of recording (gig, stream,
lecture, default), `--weight signal=value` (repeatable) overrides one, `--keywords FAILED,error` boosts windows whose
speech or on-screen text contains them. It only reads what other commands stored.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import highlights
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError
from split_video.timefmt import format_timestamp, parse_time
from split_video.timeline import TimelineError
from split_video.transcribe_cli import _asset_id, _Json, _Root, _Target

console = Console()
error_console = Console(stderr=True)

_WEIGHT_HELP = (
    f"Override one weight, e.g. --weight applause=3 (repeatable). Signals: {', '.join(highlights.WEIGHT_SIGNALS)}."
)
_EXPECTED = (highlights.HighlightsError, TimelineError, MediaError)


def _say(text: str) -> None:
    console.print(text, highlight=False, soft_wrap=True)


def _seconds(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return parse_time(text)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def highlights_cmd(
    target: str = _Target,
    root: Path = _Root,
    preset: str = typer.Option(
        "default", "--preset", help=f"Weights for the kind of recording: {', '.join(highlights.PRESETS)}."
    ),
    count: int = typer.Option(highlights.DEFAULT_COUNT, "--count", help="How many candidates at most."),
    min_seconds: float = typer.Option(highlights.DEFAULT_MIN_SECONDS, "--min-seconds", help="Shortest candidate."),
    max_seconds: float = typer.Option(highlights.DEFAULT_MAX_SECONDS, "--max-seconds", help="Longest candidate."),
    target_seconds: float | None = typer.Option(
        None, "--target-seconds", help="Window length to score (default 30, within min and max)."
    ),
    start: str | None = typer.Option(None, "--start", help="Only look from here (seconds or 1:23:45)."),
    end: str | None = typer.Option(None, "--end", help="Only look up to here."),
    keywords: str | None = typer.Option(
        None,
        "--keywords",
        help="Comma separated words to look for in speech and on-screen text (replaces the preset's).",
    ),
    weight: list[str] = typer.Option(
        [],
        "--weight",
        help=_WEIGHT_HELP,
    ),
    min_gap_seconds: float = typer.Option(
        highlights.DEFAULT_MIN_GAP, "--min-gap-seconds", help="Space between candidates."
    ),
    snap: bool = typer.Option(
        True, "--snap/--no-snap", help="Snap the edges to silences, sentences and scene changes."
    ),
    as_json: bool = _Json,
) -> None:
    """Suggest highlight candidates for TARGET from its stored signals, ranked, with the reasons for each."""
    media = LocalMediaStore(root.resolve())
    lo, hi = _seconds(start), _seconds(end)
    try:
        found = highlights.suggest_highlights(
            media,
            _asset_id(media, target),
            preset=preset,
            count=count,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            target_seconds=target_seconds,
            weights=highlights.parse_weights(",".join(weight)) if weight else None,
            keywords=keywords,
            start=lo,
            end=hi,
            min_gap_seconds=min_gap_seconds,
            snap=snap,
        )
    except _EXPECTED as exc:
        error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from None
    if as_json:
        typer.echo(highlights.dumps(found))
        return
    total = max((c["end"] for c in found["candidates"]), default=0.0)
    _say(f"[dim]preset {found['preset']}, signals: {', '.join(found['signals']['used'])}[/dim]")
    for c in found["candidates"]:
        span = f"{format_timestamp(c['start'], total)} - {format_timestamp(c['end'], total)}"
        reasons = "; ".join(r["note"] for r in c["reasons"][:3])
        _say(f"{c['rank']:>2}  {span}  {c['duration']:g}s  score {c['score']:.2f}  {escape(reasons)}")
        if c.get("title_hint"):
            _say(f"[dim]      {escape(c['title_hint'])}[/dim]")
    for note in found["notes"]:
        _say(f"[yellow]note:[/yellow] {escape(note)}")
    for item in found["signals"]["missing"]:
        _say(f"[yellow]{item['signal']}:[/yellow] {escape(item['how'])}")


def register(app: typer.Typer) -> None:
    app.command("highlights")(highlights_cmd)
