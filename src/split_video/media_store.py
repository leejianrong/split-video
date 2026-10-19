"""Where media lives: the `MediaStore` interface and an in-memory version.

The engine (`project.py`, `timeline_service.py`, `render.py`, the editor's
caches, jobs and routes, the MCP server) only ever reaches source media, the
files it writes (exports, renders, manifests) and the derived analysis caches
through a `MediaStore`, so it never builds a filesystem path itself.
`local_media_store.LocalMediaStore` keeps today's behaviour: files under a
root directory, with the caches in a `.split-video-cache/` sidecar next to
each video. A hosted deployment would supply an object-storage version of the
same interface (downloading to a scratch file where ffmpeg needs one).
`InMemoryMediaStore` is the reference implementation and what the unit tests
run the engine against.

Media is identified by an opaque `media_id` string. Today that is the path
relative to the store's root, with POSIX separators; callers must treat it as
a token (get ids from `resolve`, `list_dir` and `join`, never splice them).
Timeline clips do not reference these ids directly: they reference content-
derived asset ids (`assets.py`), and the per-workspace asset table that maps an
asset id to its current media id is persisted through `load_asset_table` and
`save_asset_table` below.

The sibling interface for project files is `store.ProjectStore`.
"""

from __future__ import annotations

import io
import json
import posixpath
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable

from split_video.ffmpeg import MediaInfo, ProbeError

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".m2ts", ".flv", ".wmv"}
# Audio files are listed too: the compose page puts them on audio tracks.
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS


class MediaError(Exception):
    """Base class for failures reaching media."""


class MediaOutsideRootError(MediaError, ValueError):
    """An id or path resolves outside the store's root (the editor answers 400)."""


class MediaNotFoundError(MediaError):
    """No such file or directory (the editor answers 404)."""


@dataclass(frozen=True)
class MediaStat:
    size: int
    mtime: float


@dataclass(frozen=True)
class BrowseEntry:
    name: str
    path: str  # the entry's media_id (a directory's id is what `list_dir` takes)
    is_dir: bool


@dataclass(frozen=True)
class BrowseListing:
    cwd: str  # "" means the root itself
    parent: str | None  # None only when cwd is the root
    entries: list[BrowseEntry]


@runtime_checkable
class MediaStore(Protocol):
    """Source media, written outputs and derived caches, by opaque `media_id`."""

    # --- identity and lookup ----------------------------------------------------

    def resolve(self, media_id: str, *, strict: bool = False) -> str:
        """The canonical id for `media_id` (symlinks followed), checking that it
        stays inside the store. Does not check that it exists. Raises
        `MediaOutsideRootError`. By default a leading "/" is ignored (ids from
        URLs and pickers are root-relative); `strict=True` takes the id literally,
        as stored in a project file."""
        ...

    def resolve_user_path(self, text: str) -> str:
        """Like `resolve`, for a path a person or agent typed: a local store also
        accepts an absolute path that lies inside its root."""
        ...

    def exists(self, media_id: str) -> bool:
        """Whether anything (file or directory) is stored at `media_id`."""
        ...

    def is_file(self, media_id: str) -> bool: ...

    def stat(self, media_id: str) -> MediaStat:
        """Size and modification time; `MediaNotFoundError` if it isn't there."""
        ...

    def probe(self, media_id: str) -> MediaInfo:
        """Duration, size and audio presence. `MediaNotFoundError` if missing,
        `ffmpeg.ProbeError` if it can't be read as media."""
        ...

    def list_dir(self, path: str = "") -> BrowseListing:
        """The media files and sub-directories of the directory `path` (hidden
        entries and non-media files left out; directories first, then by name).
        `MediaOutsideRootError` for an escape, `MediaNotFoundError` if `path`
        isn't a directory."""
        ...

    # --- ids as strings ------------------------------------------------------------

    def name(self, media_id: str) -> str:
        """The file name part of an id, e.g. 'gig.mp4' (used for labels and extensions)."""
        ...

    def join(self, parent_id: str, name: str) -> str:
        """The id of `name` inside the directory `parent_id`."""
        ...

    def export_dir(self, media_id: str) -> str:
        """The id of the directory a video's split exports go in: '<stem>_split' beside it."""
        ...

    def display_path(self, media_id: str) -> str:
        """A human-readable location for messages and job status (locally an absolute path)."""
        ...

    # --- bytes, for ffmpeg and for serving --------------------------------------

    def local_path(self, media_id: str) -> AbstractContextManager[Path]:
        """A real filesystem path holding the media, for ffmpeg/ffprobe to read.
        Valid inside the `with` block only: a local store yields the file itself, a
        hosted one would download it to scratch space and clean up on exit."""
        ...

    def derived_id(self, media_id: str, name: str) -> str:
        """The id of a derived file `name` (e.g. "proxy.mp4") for the media `media_id`: stored with
        the media, never apart from it (locally in the video's cache sidecar). It is an ordinary
        id, so `write_local`, `open_read`, `stat` and `delete` work on it; it is not listed by
        `list_dir`. Nothing here ties it to the media's content: pair it with `save_cache`."""
        ...

    def write_local(self, media_id: str) -> AbstractContextManager[Path]:
        """A real path for ffmpeg to write `media_id` to (parent directories are
        created). The store commits it when the block exits without an error, so a
        hosted store can upload it; a local one has written it in place."""
        ...

    def write_bytes(self, media_id: str, data: bytes) -> None:
        """Store `data` at `media_id`, creating parent directories (manifests)."""
        ...

    def delete(self, media_id: str) -> None:
        """Remove the media if it exists (a cancelled job's partial output). Never raises for a missing one."""
        ...

    def open_read(self, media_id: str) -> BinaryIO:
        """A seekable binary stream of the media, for the editor to serve with
        HTTP range support. `MediaNotFoundError` if it isn't there."""
        ...

    # --- derived caches ----------------------------------------------------------

    def load_cache(self, media_id: str, name: str) -> Any | None:
        """The JSON-serialisable payload cached under `name` for `media_id`, if it
        was saved for this exact content (size and mtime), else None. Never raises:
        a missing, corrupt or stale cache is just a miss."""
        ...

    def save_cache(self, media_id: str, name: str, payload: Any) -> None:
        """Replace the cache `name` for `media_id`, atomically, tagged with the
        media's current size and mtime."""
        ...

    # --- the asset table ----------------------------------------------------------

    def load_asset_table(self) -> Any | None:
        """The JSON payload saved by `save_asset_table`, or None if there is none or it
        can't be read (a damaged table is rebuilt from the clips' source hints)."""
        ...

    def save_asset_table(self, payload: Any) -> None:
        """Replace the workspace's asset table, atomically. It lives with the media
        (locally a sidecar file in the root), so it survives exactly as long as they do."""
        ...


# --- the in-memory implementation ---------------------------------------------------


@dataclass
class _Entry:
    data: bytes
    info: MediaInfo | None
    mtime: float


class InMemoryMediaStore:
    """A `MediaStore` that keeps everything in dicts.

    Media is added with `add` (bytes and/or a fake `MediaInfo` for `probe`).
    By default `local_path` and `write_local` hand out placeholder paths that
    don't exist, so a test that monkeypatches the ffmpeg calls touches no disk;
    with `materialise=True` they use real scratch files so ffmpeg can run.
    """

    def __init__(self, *, materialise: bool = False) -> None:
        self._files: dict[str, _Entry] = {}
        self._caches: dict[tuple[str, str], tuple[dict[str, float], Any]] = {}
        self._asset_table: Any | None = None
        self._materialise = materialise

    # --- setup ---------------------------------------------------------------------

    def add(self, media_id: str, data: bytes = b"", *, info: MediaInfo | None = None, mtime: float = 1.0) -> str:
        media_id = self.resolve(media_id)
        self._files[media_id] = _Entry(data, info, mtime)
        return media_id

    def read(self, media_id: str) -> bytes:
        return self._entry(media_id).data

    def rename(self, old: str, new: str) -> None:
        """Move a file to another id, as a person renaming it on disk would."""
        entry = self._entry(old)
        del self._files[self.resolve(old)]
        self._files[self.resolve(new)] = entry

    def remove(self, media_id: str) -> None:
        self._entry(media_id)
        del self._files[self.resolve(media_id)]

    def ids(self) -> list[str]:
        return sorted(self._files)

    # --- identity and lookup ----------------------------------------------------

    def resolve(self, media_id: str, *, strict: bool = False) -> str:
        if strict and media_id.startswith("/"):
            raise MediaOutsideRootError(media_id)
        rel = posixpath.normpath(media_id.lstrip("/")) if media_id.strip("/") else ""
        if rel == ".":
            rel = ""
        if rel == ".." or rel.startswith("../"):
            raise MediaOutsideRootError(media_id)
        return rel

    def resolve_user_path(self, text: str) -> str:
        return self.resolve(text)

    def _entry(self, media_id: str) -> _Entry:
        try:
            return self._files[self.resolve(media_id)]
        except KeyError:
            raise MediaNotFoundError(media_id) from None

    def _is_dir(self, media_id: str) -> bool:
        prefix = media_id + "/" if media_id else ""
        return media_id == "" or any(i.startswith(prefix) for i in self._files)

    def exists(self, media_id: str) -> bool:
        media_id = self.resolve(media_id)
        return media_id in self._files or self._is_dir(media_id)

    def is_file(self, media_id: str) -> bool:
        return self.resolve(media_id) in self._files

    def stat(self, media_id: str) -> MediaStat:
        entry = self._entry(media_id)
        return MediaStat(size=len(entry.data), mtime=entry.mtime)

    def probe(self, media_id: str) -> MediaInfo:
        entry = self._entry(media_id)
        if entry.info is None:
            raise ProbeError(f"could not read '{media_id}': no media info")
        return entry.info

    def list_dir(self, path: str = "") -> BrowseListing:
        target = self.resolve(path)
        if not self._is_dir(target) or target in self._files:
            raise MediaNotFoundError(path)
        prefix = target + "/" if target else ""
        dirs: set[str] = set()
        files: list[str] = []
        for media_id in self._files:
            if not media_id.startswith(prefix):
                continue
            head, _, rest = media_id[len(prefix) :].partition("/")
            if head.startswith("."):
                continue
            if rest:
                dirs.add(head)
            elif posixpath.splitext(head)[1].lower() in MEDIA_EXTENSIONS:
                files.append(head)
        entries = [BrowseEntry(name=d, path=prefix + d, is_dir=True) for d in sorted(dirs, key=str.lower)]
        entries += [BrowseEntry(name=f, path=prefix + f, is_dir=False) for f in sorted(files, key=str.lower)]
        parent = None if target == "" else posixpath.dirname(target)
        return BrowseListing(cwd=target, parent=parent, entries=entries)

    # --- ids as strings ------------------------------------------------------------

    def name(self, media_id: str) -> str:
        return posixpath.basename(media_id)

    def join(self, parent_id: str, name: str) -> str:
        return posixpath.join(parent_id, name) if parent_id else name

    def export_dir(self, media_id: str) -> str:
        stem = posixpath.splitext(posixpath.basename(media_id))[0]
        return self.join(posixpath.dirname(media_id), f"{stem}_split")

    def display_path(self, media_id: str) -> str:
        return f"memory:{media_id}"

    # --- bytes -----------------------------------------------------------------------

    @contextmanager
    def local_path(self, media_id: str) -> Iterator[Path]:
        entry = self._entry(media_id)
        if not self._materialise:
            yield Path("/in-memory-media") / media_id
            return
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / posixpath.basename(media_id)
            path.write_bytes(entry.data)
            yield path

    def derived_id(self, media_id: str, name: str) -> str:
        return self.join(posixpath.dirname(media_id), f".derived/{posixpath.basename(media_id)}.{name}")

    @contextmanager
    def write_local(self, media_id: str) -> Iterator[Path]:
        media_id = self.resolve(media_id)
        if not self._materialise:
            yield Path("/in-memory-media") / media_id
            self._files.setdefault(media_id, _Entry(b"", None, 1.0))
            return
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / (posixpath.basename(media_id) or "out")
            yield path
            self._files[media_id] = _Entry(path.read_bytes() if path.exists() else b"", None, 1.0)

    def write_bytes(self, media_id: str, data: bytes) -> None:
        self._files[self.resolve(media_id)] = _Entry(data, None, 1.0)

    def delete(self, media_id: str) -> None:
        self._files.pop(self.resolve(media_id), None)

    def open_read(self, media_id: str) -> BinaryIO:
        return io.BytesIO(self._entry(media_id).data)

    # --- derived caches ----------------------------------------------------------

    def _fingerprint(self, media_id: str) -> dict[str, float]:
        stat = self.stat(media_id)
        return {"size": stat.size, "mtime": stat.mtime}

    def load_cache(self, media_id: str, name: str) -> Any | None:
        try:
            fingerprint, payload = self._caches[(self.resolve(media_id), name)]
            if fingerprint != self._fingerprint(media_id):
                return None
        except (KeyError, MediaError):
            return None
        return json.loads(json.dumps(payload))  # a copy, as a real store would hand back

    def save_cache(self, media_id: str, name: str, payload: Any) -> None:
        # Round-tripped through JSON so a payload that couldn't be persisted fails here too.
        self._caches[(self.resolve(media_id), name)] = (
            self._fingerprint(media_id),
            json.loads(json.dumps(payload)),
        )

    # --- the asset table ----------------------------------------------------------

    def load_asset_table(self) -> Any | None:
        return None if self._asset_table is None else json.loads(json.dumps(self._asset_table))

    def save_asset_table(self, payload: Any) -> None:
        self._asset_table = json.loads(json.dumps(payload))  # JSON round trip, as for the caches
