"""`split-video apply-ops` and `ops-schema`: edit a timeline with a batch of operations (see `edit_ops.py`).

`apply-ops OPS` reads the ops document from a file (or `-` for stdin) and applies its operations to the existing project
it names, atomically: all of them or none. `--dry-run` runs the same engine and describes the result without writing
anything, `--continue-on-error` (dry run only) collects every independent problem instead of stopping at the first,
`--base-revision N` is the revision the batch was written against (a stale one exits with code 3, like `compose`),
`--json` prints the same JSON as the API and the MCP tool. A batch with problems exits 1 and prints the full report as JSON
on stdout; the summary goes to stderr. `ops-schema` prints the JSON Schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from split_video import cli_errors, edit_ops
from split_video.compose_cli import _fail
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.plan_cli import _read
from split_video.timeline import TimelineError

error_console = Console(stderr=True)
console = Console()


def apply_ops_cmd(
    ops: str = typer.Argument(..., help="The ops document: a JSON file, or - for stdin."),
    root: Path = typer.Option(
        Path("."), "--root", exists=True, file_okay=False, help="Directory holding the videos and project files."
    ),
    base_revision: int | None = typer.Option(
        None,
        "--base-revision",
        min=0,
        help="The revision of the project (see `compose show`) the batch was written against. If it has moved on, "
        "nothing is written and the command exits with code 3.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and describe the result without writing anything."),
    continue_on_error: bool = typer.Option(
        False, "--continue-on-error", help="With --dry-run: report every independent problem, not only the first."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON instead of a summary."),
) -> None:
    """Apply a batch of timeline edits (add_clip, trim_clip, ...) to a project, all or none; with problems, nothing is written."""
    doc = _read(ops)
    root = root.resolve()
    try:
        result = edit_ops.apply_ops(
            LocalMediaStore(root),
            LocalProjectStore(root),
            doc,
            dry_run=dry_run,
            base_revision=base_revision,
            continue_on_error=continue_on_error,
        )
    except edit_ops.OpsInvalidError as exc:
        raise cli_errors.fail(exc, as_json, always_json=True) from None
    except TimelineError as exc:
        raise _fail(exc, as_json)
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    label = "would apply" if dry_run else "applied"
    console.print(
        f"Project {result['project']!r}: {label} {result['applied']} ops; {result['clips']} clips on "
        f"{len(result['tracks'])} tracks, {result['duration']:g}s, revision {result['revision']}",
        highlight=False,
    )
    for w in result["warnings"]:
        error_console.print(
            f"[yellow]warning:[/yellow] {escape(w['path'] or '/')} {escape(w['message'])}", highlight=False
        )


def ops_schema_cmd() -> None:
    """Print the JSON Schema of the edit-operations format (the same as docs/schemas/edit-ops.v1.schema.json)."""
    typer.echo(edit_ops.schema_json(), nl=False)


def register(app: typer.Typer) -> None:
    app.command("apply-ops")(apply_ops_cmd)
    app.command("ops-schema")(ops_schema_cmd)
