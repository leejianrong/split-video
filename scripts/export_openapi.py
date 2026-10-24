"""Write the editor API's OpenAPI schema to openapi.json (or `--check` it).

Deterministic on purpose: sorted keys, 2-space indent, trailing newline, and
nothing that depends on the machine (the app is built against a placeholder
root, and the schema carries the API version, not the package version).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from split_video.editor.app import create_app
from split_video.editor.schemas import StateParams

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = REPO_ROOT / "openapi.json"


def render_schema() -> str:
    # The schema doesn't depend on the root's contents; "." just has to be a directory.
    defaults = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=30.0, padding=0.5)
    app = create_app(Path("."), defaults)
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if openapi.json is out of date")
    args = parser.parse_args()
    rendered = render_schema()
    if args.check:
        if not OPENAPI_PATH.exists() or OPENAPI_PATH.read_text() != rendered:
            print("openapi.json is out of date; run `make openapi`", file=sys.stderr)
            return 1
        return 0
    OPENAPI_PATH.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
