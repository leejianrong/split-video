"""`split-video apply-plan` and `plan-schema`: build a whole timeline from one JSON edit plan (see `edit_plan.py`).

`apply-plan PLAN` reads the plan from a file (or `-` for stdin) and creates or replaces the timeline project it names,
atomically. `--dry-run` validates and describes without writing, `--base-revision N` is the revision a `replace` plan was
based on (a stale one exits with code 3, like `compose`), `--json` prints the same JSON as the API and the MCP tool. A
plan with problems exits 1 and prints the full report (every error, with its path and code) as JSON on stdout; the
summary goes to stderr. `plan-schema` prints the plan's JSON Schema.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from split_video import cli_errors, edit_plan
from split_video.compose_cli import _fail
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.timeline import TimelineError

error_console = Console(stderr=True)
console = Console()


def _read(plan: str) -> Any:
    try:
        text = sys.stdin.read() if plan == "-" else Path(plan).read_text()
    except OSError as exc:
        raise typer.BadParameter(f"can't read {plan}: {exc.strerror or exc}") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{plan} is not valid JSON: {exc}") from None


def apply_plan_cmd(
    plan: str = typer.Argument(..., help="The plan: a JSON file, or - for stdin."),
    root: Path = typer.Option(
        Path("."), "--root", exists=True, file_okay=False, help="Directory holding the videos and project files."
    ),
    base_revision: int | None = typer.Option(
        None,
        "--base-revision",
        min=0,
        help="The revision of the project (see `compose show`) a `replace` plan was based on. If it has moved on, "
        "nothing is written and the command exits with code 3.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and describe the result without writing anything."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON instead of a summary."),
) -> None:
    """Create or replace a timeline project from ONE JSON edit plan; a plan with problems changes nothing."""
    doc = _read(plan)
    root = root.resolve()
    try:
        result = edit_plan.apply_plan(
            LocalMediaStore(root), LocalProjectStore(root), doc, dry_run=dry_run, base_revision=base_revision
        )
    except edit_plan.PlanInvalidError as exc:
        raise cli_errors.fail(exc, as_json, always_json=True) from None
    except TimelineError as exc:
        raise _fail(exc, as_json)
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    label = ("would be " if dry_run else "") + result["action"]
    console.print(
        f"Project {result['project']!r} {label}: {result['clips']} clips on {len(result['tracks'])} tracks, "
        f"{result['duration']:g}s, revision {result['revision']}",
        highlight=False,
    )
    for w in result["warnings"]:
        error_console.print(
            f"[yellow]warning:[/yellow] {escape(w['path'] or '/')} {escape(w['message'])}", highlight=False
        )


def plan_schema_cmd() -> None:
    """Print the JSON Schema of the edit plan format (the same as docs/schemas/edit-plan.v1.schema.json)."""
    typer.echo(edit_plan.schema_json(), nl=False)


def register(app: typer.Typer) -> None:
    app.command("apply-plan")(apply_plan_cmd)
    app.command("plan-schema")(plan_schema_cmd)
