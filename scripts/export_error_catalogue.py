"""Write the error catalogue to docs/errors.md (or `--check` it).

The page is generated from `split_video.error_catalogue.REGISTRY`, the same table every adapter (CLI, MCP, HTTP) reads
its codes, HTTP statuses, exit codes and suggested fixes from, so the committed file only changes when the catalogue does.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from split_video import error_catalogue

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "errors.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if docs/errors.md is out of date")
    args = parser.parse_args(argv)
    rendered = error_catalogue.render_markdown()
    if args.check:
        if not DOC_PATH.exists() or DOC_PATH.read_text() != rendered:
            print(f"{DOC_PATH.name} is out of date; run `make errors`", file=sys.stderr)
            return 1
        return 0
    DOC_PATH.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
