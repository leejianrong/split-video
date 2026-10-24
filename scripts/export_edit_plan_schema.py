"""Write the edit plan's JSON Schema to docs/schemas/edit-plan.v1.schema.json (or `--check` it).

The schema is generated from the field tables in `split_video.edit_plan`, the same ones the validator reads, so the
committed file only changes when the contract does.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from split_video import edit_plan

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / f"edit-plan.v{edit_plan.SCHEMA_VERSION}.schema.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed schema is out of date")
    args = parser.parse_args(argv)
    rendered = edit_plan.schema_json()
    if args.check:
        if not SCHEMA_PATH.exists() or SCHEMA_PATH.read_text() != rendered:
            print(f"{SCHEMA_PATH.name} is out of date; run `make schemas`", file=sys.stderr)
            return 1
        return 0
    SCHEMA_PATH.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
