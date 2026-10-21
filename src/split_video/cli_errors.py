"""How the CLI reports a failure (see `error_catalogue.py` and docs/errors.md).

`fail(exc)` prints `Error:` and `Fix:` lines on stderr and returns the `typer.Exit` to raise, with the catalogue's exit
code (1; 3 for a stale revision; 4 for a missing optional extra). When the command was run with `--json`, or with
`--json-errors` before the command name (or `SPLIT_VIDEO_JSON_ERRORS=1`), the structured payload
`{code, message, fix, details?}` is also printed to stdout, so a script reads one JSON document whether the command
worked or not.
"""

from __future__ import annotations

import json
import os

import typer
from rich.console import Console
from rich.markup import escape

from split_video.error_catalogue import ErrorInfo, describe

try:  # typer 0.27 carries its own copy of click; older versions use the installed one
    from typer._click.globals import get_current_context
except ImportError:  # pragma: no cover
    from click import get_current_context

error_console = Console(stderr=True)

JSON_ERRORS_ENV = "SPLIT_VIDEO_JSON_ERRORS"
_force_json = False


def force_json(on: bool = True) -> None:
    """Print the JSON payload for every failing command (the root option `--json-errors`)."""
    global _force_json
    _force_json = on


def wants_json(as_json: bool | None = None) -> bool:
    """True if this command's failures should also be printed as JSON on stdout."""
    if as_json or _force_json or os.environ.get(JSON_ERRORS_ENV, "").lower() in ("1", "true", "yes"):
        return True
    ctx = get_current_context(silent=True)
    return bool(ctx is not None and (ctx.params.get("as_json") or ctx.params.get("json")))


def report(info: ErrorInfo, as_json: bool | None = None, *, always_json: bool = False) -> None:
    """Print `info`: the human lines to stderr and, if asked for (or `always_json`), the JSON to stdout."""
    error_console.print(f"[red]Error:[/red] {escape(info.message)}", highlight=False, soft_wrap=True)
    for issue in info.legacy.get("errors", []):  # a rejected plan or batch: one line per problem
        error_console.print(
            f"  {escape(issue.get('path') or '/')}  {issue.get('code', '')}: {escape(issue.get('message', ''))}",
            highlight=False,
            soft_wrap=True,
        )
    error_console.print(f"[yellow]Fix:[/yellow] {escape(info.fix)}", highlight=False, soft_wrap=True)
    if always_json or wants_json(as_json):
        typer.echo(json.dumps(info.to_dict(), indent=2))


def fail(
    exc: BaseException, as_json: bool | None = None, *, always_json: bool = False, exit_code: int | None = None
) -> typer.Exit:
    """Report `exc` and return the exit to raise: `raise fail(exc, as_json)`. `exit_code` overrides the catalogue's
    (2, typer's own, for a command-line value that could not be parsed)."""
    info = describe(exc)
    report(info, as_json, always_json=always_json)
    return typer.Exit(code=info.spec.exit_code if exit_code is None else exit_code)
