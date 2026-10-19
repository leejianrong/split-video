"""`split-video library scan|list`: catalogue the media in a folder as assets.

The table lives next to the media (`<dir>/.split-video-assets.json`), so a second scan only probes
files that are new or changed, and `list` never probes at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from split_video import library
from split_video.ffmpeg import FfmpegNotFoundError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError
from split_video.timefmt import format_timestamp

console = Console()
error_console = Console(stderr=True)

library_app = typer.Typer(help="Scan a folder of recordings into a catalogue of assets.", no_args_is_help=True)

_Dir = typer.Argument(..., exists=True, file_okay=False, help="The workspace folder (the root the editor serves).")
_Json = typer.Option(False, "--json", help="Print machine-readable JSON instead of a table.")


def _print_assets(rows: list[dict]) -> None:
    if not rows:
        console.print("The library is empty. Run `split-video library scan <dir>` first.")
        return
    table = Table(title="Library")
    for col in ("Asset", "Path", "Length", "Size", "Video", "Audio"):
        table.add_column(col)
    for a in rows:
        video = "-" if not a["has_video"] else f"{a['width']}x{a['height']}" + (f" @ {a['fps']:g}" if a["fps"] else "")
        table.add_row(
            a["id"],
            a["path"] if a["present"] else f"{a['path']} (missing)",
            format_timestamp(a["duration"], a["duration"]),
            f"{a['size'] / (1 << 20):.1f} MiB",
            video,
            "yes" if a["has_audio"] else "no",
        )
    console.print(table)


def scan_cmd(
    directory: Path = _Dir,
    folder: str = typer.Option("", "--folder", help="Only scan this sub-folder of the directory."),
    as_json: bool = _Json,
) -> None:
    """Probe the new or changed media files under DIRECTORY and record them as assets."""
    media = LocalMediaStore(directory.resolve())
    try:
        report = library.scan(media, folder)
    except (MediaError, FfmpegNotFoundError) as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    if as_json:
        typer.echo(json.dumps(report.to_dict(), indent=2))
        return
    console.print(
        f"Scanned {report.total} media file(s): {len(report.added)} added, {len(report.copies)} copies/moved, "
        f"{report.unchanged} unchanged, {len(report.skipped)} skipped."
    )
    for a in report.added:
        console.print(f"  [green]+[/green] {a.path}  {a.id}")
    for item in report.skipped:
        console.print(f"  [yellow]skipped[/yellow] {item['path']}: {item['reason']}")


def list_cmd(directory: Path = _Dir, as_json: bool = _Json) -> None:
    """List the library (every asset in DIRECTORY's table). Never probes."""
    rows = library.listing(LocalMediaStore(directory.resolve()))
    if as_json:
        typer.echo(json.dumps({"assets": rows}, indent=2))
        return
    _print_assets(rows)


def register(app: typer.Typer) -> None:
    library_app.command("scan")(scan_cmd)
    library_app.command("list")(list_cmd)
    app.add_typer(library_app, name="library")
