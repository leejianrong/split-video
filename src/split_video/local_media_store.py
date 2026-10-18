"""`MediaStore` on local disk: files under a root directory.

Media ids are paths relative to `root` (POSIX separators). Derived analysis
caches live in a hidden `.split-video-cache/` directory next to the source
video, not a centralized cache dir under the user's home. That is a
deliberate choice specific to how this app is run: `make dev` starts the
editor in a `--rm`'d Docker container with only the video directory
bind-mounted (`-v "$(DIR)":/data`), so anything written outside that mount,
including a `~/.cache/`-style path inside the container, is thrown away when
the container exits. A sidecar next to the video lives in the same mount, so
it persists exactly as long as the video does. Exports go to `<video>_split/`
beside the video and renders into the root, for the same reason.

Each cache `name` ("silence", "waveform", "classification") gets its own
file rather than sharing one, since they are populated independently (the
classification one from a background analysis thread) and a shared file
would need cross-class coordination to avoid one save clobbering another's
concurrent write.

Identity is a cheap (size, mtime) fingerprint rather than a content hash: a
mismatch just means "recompute and overwrite", the same cost as a cold
cache, so a false negative isn't costly and hashing full video files would be
wasteful for a single-user local tool.

This is the only module that knows the cache directory's name or does the
root-relative path arithmetic and containment checks (symlinks are followed
before checking, so a link can't lead out of the root).
"""

from __future__ import annotations

import json
import os
import posixpath
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from split_video.ffmpeg import MediaInfo, probe_media
from split_video.media_store import (
    MEDIA_EXTENSIONS,
    BrowseEntry,
    BrowseListing,
    MediaNotFoundError,
    MediaOutsideRootError,
    MediaStat,
)

CACHE_DIR_NAME = ".split-video-cache"


class LocalMediaStore:
    """Media under `root`, with the caches in a sidecar next to each file."""

    def __init__(self, root: Path) -> None:
        # `root` as given is what `display_path` shows (the CLI roots at the
        # video's own directory, so it prints the path the user typed);
        # containment is checked against the resolved root.
        self.root = root
        self._real_root = root.resolve()
        self._probes: dict[tuple[str, int], MediaInfo] = {}
        self._probe_lock = threading.Lock()

    # --- paths -----------------------------------------------------------------

    def _path(self, media_id: str) -> Path:
        """The path of an id the store handed out. Only a lexical check: ids
        are canonical, and a symlink *inside* the root that points elsewhere
        is how a user links videos in, so it is followed here (`resolve` is
        where the escape check against links happens)."""
        parts = PurePosixPath(media_id).parts
        if media_id.startswith("/") or ".." in parts:
            raise MediaOutsideRootError(media_id)
        return self.root / media_id

    def _relative(self, path: Path) -> str:
        return "" if path == self._real_root else path.relative_to(self._real_root).as_posix()

    def _contained(self, path: Path, original: str) -> Path:
        root = self._real_root
        if path != root and root not in path.parents:
            raise MediaOutsideRootError(original)
        return path

    def resolve(self, media_id: str, *, strict: bool = False) -> str:
        if strict:
            candidate = (self._real_root / media_id).resolve()  # an absolute id replaces the root, then fails the check
        else:
            candidate = (self._real_root / media_id.lstrip("/")).resolve() if media_id else self._real_root
        return self._relative(self._contained(candidate, media_id))

    def resolve_user_path(self, text: str) -> str:
        if Path(text).is_absolute():
            return self._relative(self._contained(Path(text).resolve(), text))
        return self.resolve(text)

    def exists(self, media_id: str) -> bool:
        return self._path(media_id).exists()

    def is_file(self, media_id: str) -> bool:
        return self._path(media_id).is_file()

    def stat(self, media_id: str) -> MediaStat:
        try:
            st = self._path(media_id).stat()
        except FileNotFoundError:
            raise MediaNotFoundError(media_id) from None
        return MediaStat(size=st.st_size, mtime=st.st_mtime)

    def probe(self, media_id: str) -> MediaInfo:
        """`probe_media` on the file, remembered per (path, mtime) since the
        editor asks for the same source's duration on every project reload."""
        path = self._path(media_id)
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except FileNotFoundError:
            raise MediaNotFoundError(media_id) from None
        with self._probe_lock:
            cached = self._probes.get(key)
        if cached is None:
            cached = probe_media(path)
            with self._probe_lock:
                self._probes[key] = cached
        return cached

    def list_dir(self, path: str = "") -> BrowseListing:
        target = (self._real_root / path.lstrip("/")).resolve() if path else self._real_root
        self._contained(target, path)
        if not target.is_dir():
            raise MediaNotFoundError(path)
        entries: list[BrowseEntry] = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                entries.append(BrowseEntry(name=child.name, path=self._relative(child), is_dir=True))
            elif child.suffix.lower() in MEDIA_EXTENSIONS:
                entries.append(BrowseEntry(name=child.name, path=self._relative(child), is_dir=False))
        parent = None if target == self._real_root else self._relative(target.parent)
        return BrowseListing(cwd=self._relative(target), parent=parent, entries=entries)

    # --- ids as strings ------------------------------------------------------------

    def name(self, media_id: str) -> str:
        return posixpath.basename(media_id)

    def join(self, parent_id: str, name: str) -> str:
        return posixpath.join(parent_id, name) if parent_id else name

    def export_dir(self, media_id: str) -> str:
        stem = PurePosixPath(media_id).stem
        return self.join(posixpath.dirname(media_id), f"{stem}_split")

    def display_path(self, media_id: str) -> str:
        return str(self.root / media_id)

    # --- bytes -----------------------------------------------------------------------

    @contextmanager
    def local_path(self, media_id: str) -> Iterator[Path]:
        yield self._path(media_id)

    @contextmanager
    def write_local(self, media_id: str) -> Iterator[Path]:
        path = self._path(media_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        yield path

    def write_bytes(self, media_id: str, data: bytes) -> None:
        path = self._path(media_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def open_read(self, media_id: str) -> BinaryIO:
        try:
            return self._path(media_id).open("rb")
        except FileNotFoundError:
            raise MediaNotFoundError(media_id) from None

    # --- derived caches ----------------------------------------------------------

    def _cache_path(self, media_id: str, name: str) -> Path:
        source = self._path(media_id)
        return source.parent / CACHE_DIR_NAME / f"{source.name}.{name}.json"

    def _fingerprint(self, media_id: str) -> dict[str, float]:
        stat = self._path(media_id).stat()
        return {"size": stat.st_size, "mtime": stat.st_mtime}

    def load_cache(self, media_id: str, name: str) -> Any | None:
        try:
            with self._cache_path(media_id, name).open() as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, MediaOutsideRootError):
            return None
        try:
            if data["fingerprint"] != self._fingerprint(media_id):
                return None
            return data["payload"]
        except (KeyError, TypeError, OSError):
            return None

    def save_cache(self, media_id: str, name: str, payload: Any) -> None:
        """Written to a temp file and renamed into place so a concurrent
        `load_cache` never observes a partially-written cache file."""
        path = self._cache_path(media_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.parent / f"{path.name}.tmp"
        with tmp_path.open("w") as f:
            json.dump({"fingerprint": self._fingerprint(media_id), "payload": payload}, f, separators=(",", ":"))
        os.replace(tmp_path, path)
