"""`split-video presets`: list the export presets `compose render --preset` takes (see `presets.py`).

`--json` prints the same JSON as `GET /api/v1/presets` and the MCP tool `list_export_presets`.
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from split_video import presets

console = Console()


def presets_cmd(
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON instead of a table."),
) -> None:
    """List the export presets (size, frame rate, quality) for `compose render --preset`."""
    listing = presets.listing()
    if as_json:
        typer.echo(json.dumps(listing, indent=2))
        return
    table = Table(box=None)
    for column in ("Name", "Size", "FPS", "Video", "Audio", "Description"):
        table.add_column(column)
    for p in listing["presets"]:
        table.add_row(
            p["name"],
            f"{p['width']}x{p['height']}",
            str(p["fps"]),
            f"{p['video_codec']} {p['x264_preset']} crf {p['crf']}",
            f"{p['audio_codec']} {p['audio_bitrate']}",
            p["description"],
        )
    console.print(table)
    console.print(f"Fit when the aspect differs: {', '.join(listing['fits'])} (default {listing['default_fit']}).")
