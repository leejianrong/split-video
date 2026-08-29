"""Sidecar disk persistence for editor/cache.py's per-file analysis caches.

Cache files live in a hidden `.split-video-cache/` directory next to the
source video, not a centralized cache dir under the user's home. That's a
deliberate choice specific to how this app is actually run: `make dev`
starts the editor in a `--rm`'d Docker container with only the video
directory bind-mounted (`-v "$(DIR)":/data`), so anything written outside
that mount — including a `~/.cache/`-style path inside the container's
filesystem — is thrown away the moment the container exits. A sidecar next
to the video lives in the same mount, so it persists exactly as long as the
video does.

Each of `SilenceCache`, `WaveformCache`, and `ClassificationCache` gets its
own file (keyed by cache `name`) rather than sharing one, since they're
populated independently — `ClassificationCache` in particular is written
from a background analysis thread — and a shared file would need
cross-class coordination to avoid one save clobbering another's concurrent
write.

Identity is a cheap (size, mtime) fingerprint rather than a content hash: a
mismatch just means "recompute and overwrite", the same cost as a cold
cache, so a false negative here isn't costly and hashing full video files
would be wasteful for a single-user local tool.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CACHE_DIR_NAME = ".split-video-cache"


def _cache_path(source: Path, name: str) -> Path:
    return source.parent / CACHE_DIR_NAME / f"{source.name}.{name}.json"


def _fingerprint(source: Path) -> dict[str, float]:
    stat = source.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime}


def load(source: Path, name: str) -> Any | None:
    """The cached payload for `name` if it's on disk and its stored
    fingerprint still matches `source`, else None.

    Never raises: a missing, corrupt, or unreadable cache file is just a
    cache miss, not a reason to fail opening the file.
    """
    try:
        with _cache_path(source, name).open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    try:
        if data["fingerprint"] != _fingerprint(source):
            return None
        return data["payload"]
    except (KeyError, TypeError):
        return None


def save(source: Path, name: str, payload: Any) -> None:
    """Persist `payload` for `name`, tagged with `source`'s current
    fingerprint.

    Written to a temp file and renamed into place so a concurrent `load`
    never observes a partially-written cache file.
    """
    path = _cache_path(source, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.tmp"
    with tmp_path.open("w") as f:
        json.dump({"fingerprint": _fingerprint(source), "payload": payload}, f, separators=(",", ":"))
    os.replace(tmp_path, path)
