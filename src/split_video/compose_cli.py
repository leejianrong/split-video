"""`split-video compose ...`: build and render multi-clip timeline projects
from the command line. Thin callers of `split_video.timeline_service`, which
the editor's compose page and the MCP server use too.

A project is a named `<name>.split-video-timeline.json` file in `--root`
(default: the current directory); clip sources are paths under that root.
Clips are numbered from 1 in track order, as in the editor. Every command
takes `--json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from split_video import timeline, timeline_service
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError, ProbeError
from split_video.timefmt import format_timestamp, parse_time
from split_video.timeline import Project, TimelineError

console = Console()
error_console = Console(stderr=True)

compose_app = typer.Typer(help="Build and render multi-clip timeline projects.", no_args_is_help=True)

_Root = typer.Option(
    Path("."), "--root", exists=True, file_okay=False, help="Directory holding the videos and project files."
)
_Json = typer.Option(False, "--json", help="Print machine-readable JSON instead of a table.")
_Name = typer.Argument(..., help="Project name (no extension).")
_Clip = typer.Argument(..., min=1, help="Clip number on the video track, from 1.")

_EXPECTED_ERRORS = (TimelineError, FfmpegNotFoundError, ProbeError, ExtractError)


def _fail(exc: Exception) -> typer.Exit:
    error_console.print(f"[red]Error:[/red] {exc}")
    return typer.Exit(code=1)


def _time(text: str) -> float:
    try:
        return parse_time(text)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def _show(root: Path, name: str, project: Project, as_json: bool, **extra: Any) -> None:
    info = timeline_service.describe(root, name, project)
    if as_json:
        typer.echo(json.dumps({**extra, **info}, indent=2))
        return
    total = max(project.duration, 1.0)
    table = Table(title=f"{name} — {format_timestamp(project.duration, total)}")
    for col in ("#", "Source", "In", "Out", "Start", "Length", "Transition in"):
        table.add_column(col, justify="right" if col == "#" else "left")
    for track in info["tracks"]:
        for c in track["clips"]:
            join = "cut" if c["transition"] == "cut" else f"crossfade {c['transition_duration']:g}s"
            table.add_row(
                str(c["index"]),
                c["source"],
                format_timestamp(c["in"], total),
                format_timestamp(c["out"], total),
                format_timestamp(c["start"], total),
                format_timestamp(c["duration"], total),
                join,
            )
    console.print(table)


def _clip_id(root: Path, name: str, clip: int) -> str:
    return timeline_service.clip_id_at(timeline_service.load_existing(root, name)[1], clip)


def list_cmd(root: Path = _Root, as_json: bool = _Json) -> None:
    """List the timeline projects in the root."""
    names = timeline.list_projects(root.resolve())
    if as_json:
        typer.echo(json.dumps({"projects": names}, indent=2))
    else:
        console.print("\n".join(names) if names else "No projects yet.")


def new_cmd(name: str = _Name, root: Path = _Root, as_json: bool = _Json) -> None:
    """Create an empty project."""
    root = root.resolve()
    try:
        project = timeline_service.create_project(root, name)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc)
    _show(root, name, project, as_json)


def show_cmd(name: str = _Name, root: Path = _Root, as_json: bool = _Json) -> None:
    """Show a project's clips."""
    root = root.resolve()
    try:
        _, project = timeline_service.load_existing(root, name)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc)
    _show(root, name, project, as_json)


def add_clip_cmd(
    name: str = _Name,
    video: str = typer.Argument(..., help="Video path, relative to --root."),
    at: int | None = typer.Option(None, "--at", min=1, help="Insert as this clip number (default: at the end)."),
    in_point: str | None = typer.Option(None, "--in", help="Start within the video: seconds or [H:]MM:SS."),
    out_point: str | None = typer.Option(None, "--out", help="End within the video (default: its end)."),
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Add a clip of a video to the video track."""
    root = root.resolve()
    try:
        project = timeline_service.add_clip(
            root,
            name,
            video,
            index=None if at is None else at - 1,
            in_point=0.0 if in_point is None else _time(in_point),
            out_point=None if out_point is None else _time(out_point),
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc)
    _show(root, name, project, as_json)


def trim_cmd(
    name: str = _Name,
    clip: int = _Clip,
    in_point: str | None = typer.Option(None, "--in", help="New start within the source."),
    out_point: str | None = typer.Option(None, "--out", help="New end within the source."),
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Change which part of its source a clip uses."""
    if in_point is None and out_point is None:
        raise typer.BadParameter("pass --in and/or --out")
    root = root.resolve()
    try:
        project = timeline_service.trim_clip(
            root,
            name,
            _clip_id(root, name, clip),
            in_point=None if in_point is None else _time(in_point),
            out_point=None if out_point is None else _time(out_point),
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc)
    _show(root, name, project, as_json)


def move_cmd(
    name: str = _Name,
    clip: int = _Clip,
    to: int = typer.Option(..., "--to", min=1, help="New clip number."),
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Move a clip to another position."""
    root = root.resolve()
    try:
        project = timeline_service.move_clip(root, name, _clip_id(root, name, clip), to - 1)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc)
    _show(root, name, project, as_json)


def remove_cmd(name: str = _Name, clip: int = _Clip, root: Path = _Root, as_json: bool = _Json) -> None:
    """Remove a clip."""
    root = root.resolve()
    try:
        project = timeline_service.remove_clip(root, name, _clip_id(root, name, clip))
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc)
    _show(root, name, project, as_json)


def transition_cmd(
    name: str = _Name,
    clip: int = _Clip,
    kind: str = typer.Option(..., "--kind", help="cut or crossfade."),
    duration: float = typer.Option(1.0, "--duration", help="Crossfade length in seconds."),
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Set how a clip joins the one before it."""
    root = root.resolve()
    try:
        project = timeline_service.set_transition(
            root, name, _clip_id(root, name, clip), kind, 0.0 if kind == "cut" else duration
        )
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc)
    _show(root, name, project, as_json)


def render_cmd(
    name: str = _Name,
    output: str | None = typer.Option(None, "--output", help="Output filename (default: <name>-render.mp4)."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace the output if it exists."),
    root: Path = _Root,
    as_json: bool = _Json,
) -> None:
    """Render the project to an mp4 in the root."""
    root = root.resolve()
    try:
        project, filename, out_path = timeline_service.plan_render(root, name, output, overwrite)
        timeline_service.run_render(project, root, out_path)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc)
    if as_json:
        typer.echo(json.dumps({"name": name, "output": filename, "duration": project.duration}, indent=2))
    else:
        console.print(f"[green]Rendered[/green] {filename}")


def register(app: typer.Typer) -> None:
    for command_name, fn in (
        ("list", list_cmd),
        ("new", new_cmd),
        ("show", show_cmd),
        ("add-clip", add_clip_cmd),
        ("trim", trim_cmd),
        ("move", move_cmd),
        ("remove", remove_cmd),
        ("transition", transition_cmd),
        ("render", render_cmd),
    ):
        compose_app.command(command_name)(fn)
    app.add_typer(compose_app, name="compose")
