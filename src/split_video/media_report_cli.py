"""`split-video report`: every stored signal of a recording in one call (see `media_report.py`).

`report TARGET` prints a compact summary (what exists, the loudness, the audio class regions, silence gaps, strongest
scene changes, the transcript's keywords and one line per time bucket); `--json` prints the whole document, the same
JSON as the API and the MCP tool, as compact JSON of at most 40 KB. It only reads what other commands stored, unless
`--prepare` first makes the missing cheap signals (loudness curve, silence scan, audio classification; never
transcription, scenes or on-screen text, which `missing` lists with the command that makes them).
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import cli_errors, media_report
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.jobs import JobCancelledError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError
from split_video.timefmt import format_timestamp
from split_video.timeline import TimelineError
from split_video.transcribe_cli import _asset_id, _Json, _Root, _Target

console = Console()
error_console = Console(stderr=True)

_EXPECTED = (media_report.MediaReportError, TimelineError, MediaError, ExtractError, ProbeError, FfmpegNotFoundError)


def _say(text: str) -> None:
    console.print(text, highlight=False, soft_wrap=True)


def _cell(values: list | None, i: int, fmt: str = "{}") -> str:
    return "-" if values is None or values[i] is None else fmt.format(values[i])


def report_cmd(
    target: str = _Target,
    root: Path = _Root,
    bucket_seconds: float | None = typer.Option(
        None,
        "--bucket-seconds",
        help=(
            f"Seconds per time bucket ({media_report.MIN_BUCKET_SECONDS:g} to {media_report.MAX_BUCKET_SECONDS:g}; "
            f"default the longer of {media_report.DEFAULT_MIN_BUCKET_SECONDS:g} and duration / "
            f"{media_report.TARGET_BUCKETS}). Coarser if the report would be too large."
        ),
    ),
    include: str | None = typer.Option(
        None, "--include", help="Comma separated signals to include: audio,loudness,silence,speech,scenes,ocr."
    ),
    prepare: bool = typer.Option(
        False,
        "--prepare",
        help="First make the missing cheap signals: loudness, silence scan, audio classification (not transcripts).",
    ),
    as_json: bool = _Json,
) -> None:
    """Show everything stored about TARGET in one call: duration, silences, loudness, audio classes, scene changes,
    activity, transcript summary and on-screen text, in time buckets."""
    media = LocalMediaStore(root.resolve())
    try:
        asset_id = _asset_id(media, target)
        if prepare:
            with error_console.status(f"Preparing {target}..."):
                found = media_report.media_report(
                    media, asset_id, bucket_seconds=bucket_seconds, include=include, prepare=True
                )
        else:
            found = media_report.media_report(media, asset_id, bucket_seconds=bucket_seconds, include=include)
    except _EXPECTED as exc:
        raise cli_errors.fail(exc) from None
    except JobCancelledError:
        raise typer.Exit(code=130) from None
    if as_json:
        typer.echo(media_report.dumps(found))
        return
    _print(found)


def _print(found: dict) -> None:
    a = found["asset"]
    total = a.get("duration", 0.0)
    size = "x".join(str(a[k]) for k in ("width", "height")) if a.get("width") else "audio only"
    _say(f"[bold]{escape(str(a.get('path', a['id'])))}[/bold]  {a['id']}  {format_timestamp(total, total)}  {size}")
    notices = found["notices"]
    if notices["coarsened"]:
        c = notices["coarsened"]
        _say(
            f"[yellow]coarsened:[/yellow] buckets of {c['bucket_seconds']:g} s instead of {c['requested_bucket_seconds']:g} s to fit {found['limits']['max_bytes']} bytes"
        )
    if notices["dropped"]:
        _say(f"[yellow]dropped to fit:[/yellow] {', '.join(notices['dropped'])}")
    if found["prepared"]:
        p = found["prepared"]
        _say(
            f"prepared: {', '.join(p['prepared']) or 'nothing new'}"
            + (f" (already: {', '.join(p['already'])})" if p["already"] else "")
        )
        for s in p["skipped"]:
            _say(f"[yellow]skipped {s['signal']}:[/yellow] {escape(s['reason'])}")
    have = [s for s, on in found["signals"]["available"].items() if on]
    _say(f"signals: {', '.join(have) or 'none yet'}")
    loud = found["loudness"]
    if loud and loud["mean_db"] is not None:
        _say(f"loudness: mean {loud['mean_db']} dB, p95 {loud['p95_db']} dB, max {loud['max_db']} dB")
    if found["regions"]:
        r = found["regions"]
        by = ", ".join(f"{k} {format_timestamp(v, total)}" for k, v in r["seconds_by_class"].items())
        _say(f"audio: {by}  ({r['count']} regions of {r['min_seconds']:g} s or more)")
    if found["silence_gaps"]:
        g = found["silence_gaps"]
        _say(f"silence gaps of {g['min_seconds']:g} s or more: {g['count']}")
    if found["scene_changes"]:
        _say(f"scene changes: {found['scene_changes']['count']}")
    if found["transcript"]:
        t = found["transcript"]
        words = ", ".join(w for w, _ in t["top_keywords"])
        _say(f"transcript: {t['words']} words ({t['language'] or '?'}), keywords: {escape(words)}")
    b = found["buckets"]
    if b:
        _say(
            f"[dim]{b['count']} buckets of {b['seconds']:g} s: time  audio  dB mean/max  silent  words  cuts  text[/dim]"
        )
        for i in range(b["count"]):
            start = format_timestamp(i * b["seconds"], total)
            klass = _cell(b["audio_class"], i)
            loud_cell = f"{_cell(b['loudness_mean_db'], i)}/{_cell(b['loudness_max_db'], i)}"
            silent = _cell(b["silence_fraction"], i, "{:.0%}")
            text = (b["transcript"] or [None] * b["count"])[i] or ""
            _say(
                f"{start}  {klass}  {loud_cell}  {silent}  {_cell(b['words'], i)}  {_cell(b['scene_changes'], i)}  {escape(text)}"
            )
    for item in found["signals"]["missing"]:
        _say(f"[yellow]{item['signal']}:[/yellow] {escape(item['how'])}")


def register(app: typer.Typer) -> None:
    app.command("report")(report_cmd)
