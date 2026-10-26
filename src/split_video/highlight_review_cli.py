"""`split-video highlight-reviews`, `review-highlight` and `make-short`: the review step of the highlight suggestions
(see `highlight_review.py`).

`highlight-reviews TARGET` prints which suggestions were accepted or dismissed (and any edges the user adjusted);
`review-highlight TARGET --start S --end E --state accepted|dismissed|clear` records one (the window is the one
`highlights` printed); `make-short TARGET --range START-END ...` adds each range to the recording's own timeline as a
labelled clip cropped to 9:16 (`--clip`: the whole frame) and marks it accepted. `--json` prints the same JSON as the
API and the MCP tools; `--base-revision` guards the write, and a stale one exits with code 3.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import cli_errors, edit_ops, highlight_review
from split_video.compose_cli import _fail
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.media_store import MediaError
from split_video.timefmt import format_timestamp, parse_time
from split_video.timeline import TimelineError
from split_video.transcribe_cli import _asset_id, _Json, _Root, _Target

console = Console()


def _say(text: str) -> None:
    console.print(text, highlight=False, soft_wrap=True)


def _time(text: str, name: str) -> float:
    try:
        return parse_time(text)
    except ValueError as exc:
        raise typer.BadParameter(f"{name}: {exc}") from None


def reviews_cmd(target: str = _Target, root: Path = _Root, as_json: bool = _Json) -> None:
    """List the reviews of TARGET's highlight suggestions (accepted, dismissed, adjusted edges)."""
    media = LocalMediaStore(root.resolve())
    try:
        doc = highlight_review.load(media, _asset_id(media, target))
    except (TimelineError, MediaError) as exc:
        raise cli_errors.fail(exc, as_json) from None
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
        return
    _say(f"[dim]revision {doc['revision']}[/dim]")
    for key, entry in doc["reviews"].items():
        edges = entry.get("adjusted") or entry
        span = f"{format_timestamp(edges['start'], edges['end'])} - {format_timestamp(edges['end'], edges['end'])}"
        note = " (adjusted)" if entry.get("adjusted") else ""
        _say(f"{escape(key)}  {entry['state']}  {span}{note}")
    if not doc["reviews"]:
        _say("No suggestion has been reviewed yet.")


def review_cmd(
    target: str = _Target,
    start: str = typer.Option(
        ..., "--start", help="Start of the suggestion's scored window (as `highlights` printed)."
    ),
    end: str = typer.Option(..., "--end", help="End of the suggestion's scored window."),
    state: str = typer.Option(..., "--state", help="accepted, dismissed, pending (with adjusted edges), or clear."),
    adjusted_start: str | None = typer.Option(None, "--adjusted-start", help="Your own start (with --adjusted-end)."),
    adjusted_end: str | None = typer.Option(None, "--adjusted-end", help="Your own end."),
    base_revision: int | None = typer.Option(
        None, "--base-revision", min=0, help="The reviews' revision; a stale one exits with code 3."
    ),
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Accept or dismiss one highlight suggestion of TARGET, or clear its review."""
    media = LocalMediaStore(root.resolve())
    lo, hi = _time(start, "--start"), _time(end, "--end")
    try:
        doc = highlight_review.set_review(
            media,
            _asset_id(media, target),
            highlight_review.key_of(lo, hi),
            None if state == "clear" else state,
            start=lo,
            end=hi,
            adjusted_start=None if adjusted_start is None else _time(adjusted_start, "--adjusted-start"),
            adjusted_end=None if adjusted_end is None else _time(adjusted_end, "--adjusted-end"),
            base_revision=base_revision,
        )
    except (TimelineError, MediaError) as exc:
        raise _fail(exc, as_json)
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
        return
    _say(f"{highlight_review.key_of(lo, hi)}: {state}; revision {doc['revision']}")


def make_short_cmd(
    target: str = _Target,
    ranges: list[str] = typer.Option(
        ..., "--range", help="START-END of a highlight, e.g. 4:10-4:58 (repeatable; in this order, at the end)."
    ),
    clip: bool = typer.Option(False, "--clip", help="Whole frame instead of a 9:16 crop."),
    video: str | None = typer.Option(
        None, "--video", help="The recording whose timeline to edit (default: TARGET, when it is a file)."
    ),
    base_revision: int | None = typer.Option(
        None, "--base-revision", min=0, help="The timeline's revision (compose show --video); stale exits with code 3."
    ),
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Add highlight ranges of TARGET to the recording's own timeline as labelled clips (9:16 shorts), accepting them."""
    root = root.resolve()
    media, store = LocalMediaStore(root), LocalProjectStore(root)
    items = []
    for text in ranges:
        lo, _, hi = text.rpartition("-")
        if not lo:
            raise typer.BadParameter(f"--range {text!r} must look like START-END, e.g. 4:10-4:58")
        items.append({"start": _time(lo, "--range"), "end": _time(hi, "--range")})
    try:
        asset_id = _asset_id(media, target)
        if video is None and asset_id == target:
            raise typer.BadParameter("give --video when TARGET is an asset id")
        source = media.resolve_user_path(video or target)
        result = highlight_review.make_clips(
            media, store, source, asset_id, items, "clip" if clip else "short", base_revision=base_revision
        )
    except edit_ops.OpsInvalidError as exc:
        raise cli_errors.fail(exc, as_json, always_json=True) from None
    except (TimelineError, MediaError) as exc:
        raise _fail(exc, as_json)
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    _say(f"Added {len(result['made'])} clips; revision {result['revision']}")
    for made in result["made"]:
        _say(f"  {escape(made['label'])}  {made['start']:g}s - {made['end']:g}s")


def register(app: typer.Typer) -> None:
    app.command("highlight-reviews")(reviews_cmd)
    app.command("review-highlight")(review_cmd)
    app.command("make-short")(make_short_cmd)
