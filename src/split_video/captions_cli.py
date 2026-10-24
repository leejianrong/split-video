"""`split-video caption-styles`: list the styles `compose render --captions` takes (see `captions.py`).

`--json` prints the same JSON as `GET /api/v1/caption-styles` and the MCP tool `list_caption_styles`.
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from split_video import captions

console = Console()


def caption_styles_cmd(
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON instead of a table."),
) -> None:
    """List the caption styles (size, colours, position) for `compose render --captions`."""
    listing = captions.listing()
    if as_json:
        typer.echo(json.dumps(listing, indent=2))
        return
    table = Table(box=None)
    for column in ("Name", "Size", "Text", "Highlight", "Position", "Rows", "Description"):
        table.add_column(column)
    for s in listing["styles"]:
        table.add_row(
            s["name"],
            f"{s['font_size']:g} of height",
            s["primary"],
            s["highlight"],
            s["position"],
            f"{s['max_lines']} x {s['max_chars']}",
            s["description"],
        )
    console.print(table)
    console.print(
        f"Overrides: --caption-font-size, --caption-highlight, --caption-position ({', '.join(listing['positions'])})."
    )
