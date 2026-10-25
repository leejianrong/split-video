"""Fail unless the built wheel and sdist contain the built frontend (docs/adr/0004-frontend-stack-and-structure.md).

Without this a release built before `npm run build` ships a package whose /app/ answers 404.
Usage: uv build --out-dir dist && uv run python scripts/check_wheel_contents.py dist
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

INDEX = "split_video/editor/static_app/index.html"
ASSETS = "split_video/editor/static_app/assets/"


def problems(names: list[str], label: str) -> list[str]:
    """Missing pieces in one archive's member names (`src/` prefixes in an sdist are tolerated)."""
    flat = [n.split("src/", 1)[-1] if "src/" in n else n for n in names]
    found = []
    if INDEX not in flat:
        found.append(f"{label}: missing {INDEX}")
    if not any(n.startswith(ASSETS) and n.endswith((".js", ".css")) and "-" in n for n in flat):
        found.append(f"{label}: no hashed asset under {ASSETS}")
    return found


def main(argv: list[str]) -> int:
    dist = Path(argv[1] if len(argv) > 1 else "dist")
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if not wheels or not sdists:
        print(f"expected a wheel and an sdist in {dist}", file=sys.stderr)
        return 2
    found: list[str] = []
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as zf:
            found += problems(zf.namelist(), wheel.name)
    for sdist in sdists:
        with tarfile.open(sdist) as tf:
            found += problems(tf.getnames(), sdist.name)
    for line in found:
        print(line, file=sys.stderr)
    if found:
        print("Run `npm ci && npm run build` in frontend/ before `uv build`.", file=sys.stderr)
        return 1
    print("wheel and sdist contain the built frontend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
