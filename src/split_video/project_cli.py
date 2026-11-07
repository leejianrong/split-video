"""Project-aware CLI subcommands: read and edit a video's saved split points
(the video's timeline project, `<video>.split-video-timeline.json`, which the visual editor also uses;
an old `.split-video-project.json` is migrated into it on the first edit)
without a browser. Each is a thin caller of `split_video.project`; the
editor and the MCP server call the same functions.

Every command takes `--json` for machine-readable output (agents, scripts).
Segment and split numbers are 1-based, as in the editor's segment table.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from split_video import cli_errors, project
from split_video.compose_cli import _show
from split_video.editor.cache import ClassificationCache
from split_video.editor.classify import ModelNotFoundError
from split_video.editor.job_routes import CANCEL_WAIT_SECONDS, job_out
from split_video.error_catalogue import ErrorInfo
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.job_work import submit_split
from split_video.jobs import InProcessJobQueue
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.media_store import MediaStore
from split_video.store import ProjectStore, SavedSegment
from split_video.timefmt import format_timestamp, parse_time
from split_video.timeline import TimelineError

console = Console()
error_console = Console(stderr=True)

_Source = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="The video/audio file.")
_Json = typer.Option(False, "--json", help="Print machine-readable JSON instead of a table.")

_BaseRevision = typer.Option(
    None,
    "--base-revision",
    min=0,
    help="The project revision (shown by `segments`) this change is based on. If the project has changed "
    "since, nothing is written and the command exits with code 3. Omit to edit the latest version.",
)

_EXPECTED_ERRORS = (project.ProjectError, FfmpegNotFoundError, ProbeError, ExtractError, ModelNotFoundError)

CONFLICT_EXIT_CODE = 3


def _fail(exc: Exception, as_json: bool = False, source: Path | None = None) -> typer.Exit:
    """Report `exc` (the error catalogue's `Error:` / `Fix:` lines, plus the JSON payload with `--json`) and return
    the exit to raise; a revision conflict exits with code 3."""
    return cli_errors.fail(exc, as_json)


def _stores(source: Path) -> tuple[ProjectStore, MediaStore, str]:
    """The stores holding `source`'s project and media, and its id in both: the
    sidecars sit next to the video, so the video's directory is the root."""
    return LocalProjectStore(source.parent), LocalMediaStore(source.parent), source.name


def _has_project(source: Path) -> bool:
    store, _, source_id = _stores(source)
    return project.has_project(store, source_id)


def _current(source: Path) -> list[SavedSegment]:
    store, media, source_id = _stores(source)
    return project.current_segments(store, media, source_id)


def _revision(source: Path) -> int:
    store, _, source_id = _stores(source)
    return project.current_revision(store, source_id)


def _save(source: Path, segments: list[SavedSegment], base_revision: int | None = None) -> int:
    store, media, source_id = _stores(source)
    return project.save_segments(store, source_id, segments, base_revision, media)


def _edit(
    source: Path, change: Callable[[list[SavedSegment]], list[SavedSegment]], base_revision: int | None
) -> tuple[list[SavedSegment], int]:
    """Apply `change` to the saved segments and save, atomically; returns them and the new revision."""
    store, media, source_id = _stores(source)
    saved = project.edit_segments(store, media, source_id, change, base_revision)
    return saved.value, saved.revision


def _time(text: str) -> float:
    try:
        return parse_time(text)
    except ValueError as exc:
        raise cli_errors.fail(exc, exit_code=2) from None


def _emit_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, indent=2))


def _print_table(segments: list[SavedSegment], total: float, title: str) -> None:
    table = Table(title=title)
    for col in ("#", "Start", "End", "Length", "Label", "Color", "Export", "Name"):
        table.add_column(col, justify="right" if col == "#" else "left")
    for i, s in enumerate(segments):
        table.add_row(
            str(i + 1),
            format_timestamp(s.start, total),
            format_timestamp(s.end, total),
            format_timestamp(s.end - s.start, total),
            s.label,
            s.color or "",
            "yes" if s.included else "no",
            s.export_name or "",
        )
    console.print(table)


def _report(source: Path, segments: list[SavedSegment], as_json: bool, title: str, revision: int, **extra: Any) -> None:
    if as_json:
        _emit_json(
            {"source": str(source), "revision": revision, **extra, "segments": project.describe_segments(segments)}
        )
    else:
        _print_table(segments, segments[-1].end, f"{title} — revision {revision}")


def segments_cmd(source: Path = _Source, as_json: bool = _Json) -> None:
    """Show the saved split points (or the whole recording as one segment if none are saved)."""
    try:
        saved = _has_project(source)
        segments = _current(source)
        revision = _revision(source)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json, source)
    _report(
        source,
        segments,
        as_json,
        f"Segments ({len(segments)})" + ("" if saved else " — not saved yet"),
        revision,
        saved=saved,
    )


def propose_cmd(
    source: Path = _Source,
    silence_threshold: float = typer.Option(-35.0, "--silence-threshold", help="Silence level in dB."),
    min_silence_duration: float = typer.Option(2.0, "--min-silence-duration", help="Shortest gap between songs (s)."),
    min_song_length: float = typer.Option(30.0, "--min-song-length", help="Shortest song kept on its own (s)."),
    silence_padding: float = typer.Option(0.15, "--silence-padding", help="Silence kept on each side of a cut (s)."),
    label_prefix: str | None = typer.Option(
        None, "--label-prefix", help='Label the pieces "<prefix> 1", "<prefix> 2" ... (for example Song).'
    ),
    apply: bool = typer.Option(False, "--apply", help="Save the proposal as the project, replacing any saved splits."),
    base_revision: int | None = _BaseRevision,
    as_json: bool = _Json,
) -> None:
    """Propose splits by silence detection. Shows them; saves only with --apply."""
    try:
        _, media, source_id = _stores(source)
        segments = project.propose_segments(
            media,
            source_id,
            silence_threshold=silence_threshold,
            min_silence_duration=min_silence_duration,
            min_song_length=min_song_length,
            padding=silence_padding,
            label_prefix=label_prefix,
        )
        revision = _save(source, segments, base_revision) if apply else _revision(source)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json, source)
    _report(source, segments, as_json, f"Proposed segments ({len(segments)})", revision, applied=apply)
    if apply and not as_json:
        console.print("[green]Saved[/green] — replaced the project's splits (labels and exclusions reset).")


def split_by_silence_cmd(
    source: Path = _Source,
    silence_threshold: float = typer.Option(-35.0, "--silence-threshold", help="Silence level in dB."),
    min_silence_duration: float = typer.Option(2.0, "--min-silence-duration", help="Shortest gap between songs (s)."),
    min_song_length: float = typer.Option(30.0, "--min-song-length", help="Shortest song kept on its own (s)."),
    silence_padding: float = typer.Option(0.15, "--silence-padding", help="Silence kept on each side of a cut (s)."),
    label_prefix: str | None = typer.Option(
        None, "--label-prefix", help='Label the pieces "<prefix> 1", "<prefix> 2" ... (for example Song).'
    ),
    base_revision: int | None = _BaseRevision,
    as_json: bool = _Json,
) -> None:
    """Split a video at its silences and save the pieces as its segments, as a `split` job (what `POST /assets/{id}/split`
    queues). The command waits for the job and prints it (`--json`: the job as `GET /jobs/{id}` shows it, its `result`
    is `{video, segments, revision, labels}`); Ctrl+C cancels it and saves nothing (exit 1)."""
    jobs = InProcessJobQueue(max_workers=1)
    try:
        store, media, source_id = _stores(source)
        job_id = submit_split(
            jobs,
            media,
            store,
            source_id,
            silence_threshold=silence_threshold,
            min_silence_duration=min_silence_duration,
            min_song_length=min_song_length,
            padding=silence_padding,
            label_prefix=label_prefix,
            base_revision=base_revision,
        )
        try:
            info = jobs.wait(job_id)
        except KeyboardInterrupt:
            jobs.cancel(job_id)
            info = jobs.wait(job_id, CANCEL_WAIT_SECONDS)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json, source)
    finally:
        jobs.shutdown()
    if info.state == "error":
        failure = info.error_info or {}
        error = ErrorInfo(failure["code"], failure["message"], failure["fix"])
        cli_errors.report(error, as_json)
        raise typer.Exit(code=error.spec.exit_code)
    if as_json:
        _emit_json(job_out(info).model_dump())
    if info.state != "done":
        error_console.print("[yellow]Cancelled[/yellow] — nothing was saved.")
        raise typer.Exit(code=1)
    if not as_json:
        console.print(f"Saved {info.result['segments']} segment(s) as revision {info.result['revision']}.")


def add_asset_clip_cmd(
    source: Path = _Source,
    asset: str = typer.Option(..., "--asset", help="An asset id from `library list`."),
    at: int | None = typer.Option(None, "--at", min=1, help="Insert as this clip number (default: the end)."),
    base_revision: int | None = _BaseRevision,
    as_json: bool = _Json,
) -> None:
    """Add a library asset as a clip on the video's own main track (what `POST /project/clips` does)."""
    try:
        store, media, source_id = _stores(source)
        saved = project.add_asset_clip(
            store, media, source_id, asset, index=None if at is None else at - 1, base_revision=base_revision
        )
    except (*_EXPECTED_ERRORS, TimelineError) as exc:
        raise _fail(exc, as_json, source)
    _show(store, media, source_id, saved, as_json, "split")


def add_split_cmd(
    source: Path = _Source,
    at: str = typer.Option(..., "--at", help="Where to cut: seconds (83.5) or a clock time (1:23.5)."),
    base_revision: int | None = _BaseRevision,
    as_json: bool = _Json,
) -> None:
    """Add a split at a time, cutting the segment that contains it in two."""
    position = _time(at)
    result: dict[str, int] = {}

    def change(segments: list[SavedSegment]) -> list[SavedSegment]:
        new, result["split"] = project.add_split(segments, position)
        return new

    try:
        segments, revision = _edit(source, change, base_revision)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json, source)
    split = result["split"]
    _report(source, segments, as_json, f"Added split {split} at {position:g}s", revision, split=split)


def move_split_cmd(
    source: Path = _Source,
    split: int = typer.Argument(..., min=1, help="Which split (1 = the boundary between segments 1 and 2)."),
    to: str = typer.Option(..., "--to", help="New position: seconds or a clock time."),
    base_revision: int | None = _BaseRevision,
    as_json: bool = _Json,
) -> None:
    """Move an existing split. It stops short of its neighbors rather than crossing them."""
    target = _time(to)
    result: dict[str, float] = {}

    def change(segments: list[SavedSegment]) -> list[SavedSegment]:
        new, result["applied"] = project.move_split(segments, split, target)
        return new

    try:
        segments, revision = _edit(source, change, base_revision)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json, source)
    applied = result["applied"]
    _report(
        source, segments, as_json, f"Moved split {split} to {applied:g}s", revision, split=split, applied_to=applied
    )


def remove_split_cmd(
    source: Path = _Source,
    split: int = typer.Argument(..., min=1, help="Which split to remove (merges the two segments around it)."),
    base_revision: int | None = _BaseRevision,
    as_json: bool = _Json,
) -> None:
    """Remove a split, merging the two segments either side of it."""
    try:
        segments, revision = _edit(source, lambda current: project.remove_split(current, split), base_revision)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json, source)
    _report(source, segments, as_json, f"Removed split {split}", revision, split=split)


def label_cmd(
    source: Path = _Source,
    segment: int = typer.Argument(..., min=1, help="Which segment (1-based)."),
    label: str | None = typer.Option(None, "--label", help="Display label for the segment."),
    color: str | None = typer.Option(None, "--color", help="Band color as #rrggbb; pass '' to clear."),
    include: bool | None = typer.Option(None, "--include/--exclude", help="Whether a plain export writes it."),
    export_name: str | None = typer.Option(
        None, "--export-name", help="Output filename (no extension); pass '' for the default numbered name."
    ),
    base_revision: int | None = _BaseRevision,
    as_json: bool = _Json,
) -> None:
    """Set a segment's label, color, include/exclude flag or export name. Omitted fields are unchanged."""
    if label is None and color is None and include is None and export_name is None:
        raise typer.BadParameter("give at least one of --label, --color, --include/--exclude, --export-name")
    try:
        segments, revision = _edit(
            source,
            lambda current: project.update_segment(
                current,
                segment,
                label=label,
                color=project.UNSET if color is None else color,
                included=include,
                export_name=project.UNSET if export_name is None else export_name,
            ),
            base_revision,
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json, source)
    _report(source, segments, as_json, f"Updated segment {segment}", revision, segment=segment)


def analyze_cmd(
    source: Path = _Source,
    force: bool = typer.Option(False, "--force", help="Re-run even if a cached analysis exists."),
    as_json: bool = _Json,
) -> None:
    """Classify the audio (music/speech/applause/...) with YAMNet and cache it next to the video.

    Needs the YAMNet model (`make fetch-model`; the Docker image has it). Slow on long recordings.
    """
    try:
        _, media, source_id = _stores(source)
        cache = ClassificationCache(media, source_id)
        if force or not cache.is_analyzed:
            with console.status("Analyzing audio…") if not as_json else nullcontext():
                project.run_analysis(media, source_id, cache, cache.get_thresholds())
        summary = project.summarize_classification(cache)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc, as_json, source)
    if as_json:
        _emit_json({"source": str(source), **summary})
        return
    table = Table(title="Audio classification (seconds by bucket)")
    table.add_column("Bucket")
    table.add_column("Seconds", justify="right")
    for bucket, seconds in summary["seconds_by_bucket"].items():
        table.add_row(bucket, f"{seconds:g}")
    console.print(table)


def register(app: typer.Typer) -> None:
    app.command("segments")(segments_cmd)
    app.command("propose")(propose_cmd)
    app.command("split-by-silence")(split_by_silence_cmd)
    app.command("add-asset-clip")(add_asset_clip_cmd)
    app.command("add-split")(add_split_cmd)
    app.command("move-split")(move_split_cmd)
    app.command("remove-split")(remove_split_cmd)
    app.command("label")(label_cmd)
    app.command("analyze")(analyze_cmd)
