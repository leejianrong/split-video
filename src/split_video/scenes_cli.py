"""`split-video scenes`: scene changes and a screen-activity curve for a video (see `scenes.py`).

Takes a file under `--root` (default: the current directory) or an asset id (`a_...`, from `library list`), makes the
signals (from the asset's proxy if it has a current one, else the original) and stores them next to the media; a
current set made with the same parameters is reused. `--json` prints the stored document (`scenes.py`), cut to
`--start` / `--end` if given, plus `made`. An audio-only file prints `{"skipped": "audio-only asset"}`.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import scenes
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.jobs import JobCancelledError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError
from split_video.timefmt import format_timestamp
from split_video.timeline import TimelineError
from split_video.transcribe_cli import _asset_id

console = Console()
error_console = Console(stderr=True)

_EXPECTED = (TimelineError, MediaError, ExtractError, ProbeError, FfmpegNotFoundError)


def scenes_cmd(
    target: str = typer.Argument(..., help="A video file under --root, or an asset id (a_..., see `library list`)."),
    root: Path = typer.Option(
        Path("."), "--root", exists=True, file_okay=False, help="The workspace folder (the media root)."
    ),
    threshold: float = typer.Option(
        scenes.DEFAULT_THRESHOLD, "--threshold", min=0.0, max=1.0, help="Cut score (0..1) a frame needs to count."
    ),
    min_gap: float = typer.Option(
        scenes.DEFAULT_MIN_GAP, "--min-gap", min=0.0, help="Cuts closer than this many seconds count as one."
    ),
    screen_threshold: float = typer.Option(
        scenes.DEFAULT_SCREEN_THRESHOLD,
        "--screen-threshold",
        min=0.0,
        max=1.0,
        help="Share of the picture (0..1) that must change in one frame to count as a text-only screen change; 0 = off.",
    ),
    start: float | None = typer.Option(None, "--start", help="Only show signals from this second."),
    end: float | None = typer.Option(None, "--end", help="Only show signals up to this second."),
    force: bool = typer.Option(False, "--force", help="Redo it even if current signals exist."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Find TARGET's scene changes and screen-activity curve and store them next to it."""
    media = LocalMediaStore(root.resolve())
    try:
        asset_id = _asset_id(media, target)
        with error_console.status(f"Analysing {target}..."):
            result = scenes.analyze(
                media,
                asset_id,
                threshold=threshold,
                min_gap=min_gap,
                screen_threshold=screen_threshold,
                force=force,
            )
        if "skipped" in result:
            if as_json:
                typer.echo(json.dumps(result, indent=2))
            else:
                console.print(f"Skipped {result['asset']}: {result['skipped']} (no picture to analyse).")
            return
        document = scenes.excerpt(scenes.load(media, asset_id) or {}, start, end)
    except _EXPECTED as exc:
        error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from None
    except JobCancelledError:
        raise typer.Exit(code=130) from None
    if as_json:
        typer.echo(json.dumps({**document, "made": result["made"]}, indent=2))
        return
    verb = "Analysed" if result["made"] else "Already analysed"
    console.print(
        f"{verb} {result['asset']} from the {result['source']}: {result['scenes']} scene change(s) over "
        f"{format_timestamp(result['duration'], result['duration'])}."
    )
    for s in document["scenes"]:
        kind = s.get("kind", "cut")
        changed = f", {s['changed']:.1%} of the picture changed" if "changed" in s else ""
        console.print(
            f"  {format_timestamp(s['time'], result['duration'])}  {kind}  score {s['score']:.2f}{changed}",
            highlight=False,
        )
