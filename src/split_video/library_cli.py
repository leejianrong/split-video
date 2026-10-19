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

from split_video import library, proxy
from split_video.ffmpeg import ExtractError, FfmpegNotFoundError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import MediaError
from split_video.timefmt import format_timestamp
from split_video.timeline import TimelineError

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
    for col in ("Asset", "Path", "Length", "Size", "Video", "Audio", "Px"):
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
            "-" if not a["has_video"] else "yes" if a["proxy"] and a["thumbnails"] else "no",
        )
    console.print(table)


def scan_cmd(
    directory: Path = _Dir,
    folder: str = typer.Option("", "--folder", help="Only scan this sub-folder of the directory."),
    proxies: bool = typer.Option(
        False, "--proxies", help="Also make a proxy and thumbnail strip for each newly added video (slow)."
    ),
    as_json: bool = _Json,
) -> None:
    """Probe the new or changed media files under DIRECTORY and record them as assets."""
    media = LocalMediaStore(directory.resolve())
    try:
        report = library.scan(media, folder)
    except (MediaError, FfmpegNotFoundError) as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    prepared = _prepare(media, [a.id for a in report.added if a.has_video]) if proxies else []
    if as_json:
        typer.echo(json.dumps({**report.to_dict(), **({"proxies": prepared} if proxies else {})}, indent=2))
        return
    console.print(
        f"Scanned {report.total} media file(s): {len(report.added)} added, {len(report.copies)} copies/moved, "
        f"{report.unchanged} unchanged, {len(report.skipped)} skipped."
    )
    for a in report.added:
        console.print(f"  [green]+[/green] {a.path}  {a.id}")
    for item in report.skipped:
        console.print(f"  [yellow]skipped[/yellow] {item['path']}: {item['reason']}")


def _prepare(media: LocalMediaStore, asset_ids: list[str], force: bool = False) -> list[dict]:
    """Make each asset's proxy and strip, printing progress to stderr; a failure is reported and the rest go on."""
    results = []
    for asset_id in asset_ids:
        try:
            with error_console.status(f"Making proxy for {asset_id}..."):
                results.append(proxy.prepare(media, asset_id, force=force))
        except (MediaError, TimelineError, ExtractError, FfmpegNotFoundError) as exc:
            error_console.print(f"[red]Error:[/red] {asset_id}: {exc}")
            results.append({"asset": asset_id, "error": str(exc)})
    return results


def proxies_cmd(
    directory: Path = _Dir,
    asset: str = typer.Option("", "--asset", help="Only this asset id (default: every video that lacks a proxy)."),
    force: bool = typer.Option(False, "--force", help="Rebuild even if a current proxy and strip exist."),
    as_json: bool = _Json,
) -> None:
    """Make the proxy (small seek-friendly H.264) and thumbnail strip of the videos in DIRECTORY's library."""
    media = LocalMediaStore(directory.resolve())
    ids = [asset] if asset else proxy.pending(media)
    if force and not asset:
        ids = [a["id"] for a in library.listing(media) if a["has_video"] and a["present"]]
    results = _prepare(media, ids, force)
    if as_json:
        typer.echo(json.dumps({"assets": results}, indent=2))
    else:
        for r in results:
            if "error" in r:
                console.print(f"  [red]failed[/red] {r['asset']}: {r['error']}")
            elif r.get("skipped"):
                console.print(f"  [yellow]skipped[/yellow] {r['asset']}: {r['skipped']}")
            else:
                console.print(f"  [green]ok[/green] {r['asset']}  made: {', '.join(r['made']) or 'nothing (current)'}")
        if not results:
            console.print("Every video already has a proxy and thumbnails.")
    if any("error" in r for r in results):
        raise typer.Exit(code=1)


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
    library_app.command("proxies")(proxies_cmd)
    app.add_typer(library_app, name="library")
