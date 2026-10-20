"""Assets: stable, content-derived ids for the media files timeline clips use.

A clip used to store the root-relative path of its source, which breaks when
the file is renamed or moved and can't survive a move to hosted storage. Clips
now reference an *asset id* instead, and a per-workspace *asset table* says
where that asset currently lives (plus what probing learned about it). The
table is persisted through `MediaStore.load_asset_table` / `save_asset_table`
(locally `<root>/.split-video-assets.json`); this module holds the rules and
builds no paths, so it runs unchanged on any `MediaStore`.

Asset ids
---------
`a_` plus 16 hex digits of a SHA-256 over the file's size and a *sample* of
its bytes: the whole file when it is at most 3 MiB, otherwise its first,
middle and last 1 MiB. Reading a multi-GB recording in full on every
registration would be far too slow, so this is a deliberate trade-off: two
different files with the same size and identical first, middle and last
MiB (say, the same recording with a few bytes edited in the unsampled parts,
or a re-mux that only changes metadata elsewhere) get the same id, and a
file edited only outside the samples is not noticed as changed. For real media
that has to match in size and in 3 MiB spread over the file, that is a risk
worth taking. Byte-identical files always share an id, whatever their names
and folders. All empty files share one id as well (an empty file isn't media).

Two copies of one file
----------------------
Identical bytes at two paths are one asset. Its `path` is the first one
registered; registering a copy returns the existing asset, leaves `path`
alone while the file there is still in place with the same content, and
remembers the copy's location (with its size and mtime) in the row's `copies`.
A clip keeps the path it was added with as its `source` hint, and that hint is
what it resolves to while it is a known, unchanged copy; otherwise it resolves
to `path`. If the file at `path` is gone or changed, the asset moves to
wherever it is found: relinking tries the clip's hint, then scans the
workspace and picks the match closest to the root (then alphabetical).

The table and resolution
------------------------
A row records the id, the current `path` (a media id), `size`, `mtime`,
`duration` and stream info (`has_audio`, `width`, `height`, `fps`).
Registering is idempotent and cheap on a second call: a path whose size and
mtime match its row is not hashed again.

To find a clip's file: asset id -> the table's path. If that file is gone, or
its size/mtime changed and the content no longer matches the id, the asset is
*relinked*: the workspace is scanned for a file whose size matches the row and
whose sampled hash gives the id, and the row is updated. A clip's `source` is
only a hint of the last known path; it is refreshed whenever the project is
linked or edited. Nothing found raises `SourceNotFoundError` naming the id and
the last known path.

Migrating version 1 timelines
-----------------------------
A version 1 clip has `source` and no asset. `link_project` registers each
distinct existing source file and gives the clip its asset id. If the file is
missing the clip is kept as it is (a `source`, an empty `asset`): it is
"unlinked", and is linked the next time the project is loaded or edited with
the file back in place. A table lost or damaged is rebuilt the same way,
from the clips' `source` hints (or by scanning, for a renamed file).

The table is read-modify-written under one process-wide lock and replaced
atomically. Two processes (say the CLI and the editor) racing can lose a row;
it comes back the next time a clip that needs it is linked, since every row is
derivable from the media.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from split_video.ffmpeg import MediaInfo, ProbeError
from split_video.media_store import MediaError, MediaStat, MediaStore
from split_video.timeline import AssetNotFoundError, Project, SourceNotFoundError, TimelineError, source_media_id

TABLE_VERSION = 1
ID_PREFIX = "a_"
ID_HEX_DIGITS = 16
SAMPLE_BYTES = 1 << 20  # 1 MiB from each of the start, middle and end
_ID_RE = re.compile(rf"^{ID_PREFIX}[0-9a-f]{{{ID_HEX_DIGITS}}}$")
MAX_SCAN_DEPTH = 32

_lock = threading.RLock()


def is_asset_id(text: str) -> bool:
    return bool(_ID_RE.match(text))


@dataclass(frozen=True)
class Asset:
    id: str
    path: str  # the file's media id (root-relative locator) as of `mtime`
    size: int
    mtime: float
    duration: float
    has_audio: bool
    width: int | None = None  # None: no video stream
    height: int | None = None
    fps: float | None = None
    # Other places in the workspace known to hold the same bytes: path -> (size, mtime).
    copies: dict[str, tuple[int, float]] = field(default_factory=dict)

    @property
    def has_video(self) -> bool:
        return self.width is not None

    @property
    def info(self) -> MediaInfo:
        return MediaInfo(self.duration, self.width, self.height, self.has_audio, self.fps)

    def to_dict(self) -> dict[str, Any]:
        """The row as stored in the asset table."""
        row: dict[str, Any] = {
            "path": self.path,
            "size": self.size,
            "mtime": self.mtime,
            "duration": self.duration,
            "has_audio": self.has_audio,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
        }
        if self.copies:
            row["copies"] = {p: [size, mtime] for p, (size, mtime) in sorted(self.copies.items())}
        return row

    def summary(self, present: bool) -> dict[str, Any]:
        """The asset as the CLI, API and MCP show it: the row with `copies` as a list of paths, and `present`."""
        row = self.to_dict()
        row.pop("copies", None)
        return {
            "id": self.id,
            **row,
            "has_video": self.has_video,
            "copies": sorted(self.copies),
            "present": present,
        }


class Located(NamedTuple):
    """An asset and the path a clip should use for it: its own hint if that is a known copy, else the asset's path."""

    asset: Asset
    source: str


def _asset_from_dict(asset_id: str, row: dict[str, Any]) -> Asset:
    def opt(key: str, kind: type) -> Any:
        return None if row.get(key) is None else kind(row[key])

    return Asset(
        id=asset_id,
        path=str(row["path"]),
        size=int(row["size"]),
        mtime=float(row["mtime"]),
        duration=float(row["duration"]),
        has_audio=bool(row["has_audio"]),
        width=opt("width", int),
        height=opt("height", int),
        fps=opt("fps", float),
        copies={str(p): (int(v[0]), float(v[1])) for p, v in (row.get("copies") or {}).items()},
    )


def sample_id(media: MediaStore, media_id: str, size: int) -> str:
    """The asset id of the file `media_id` (`size` bytes): see the module docstring."""
    digest = hashlib.sha256()
    digest.update(f"{size}\0".encode())
    with media.open_read(media_id) as f:
        if size <= 3 * SAMPLE_BYTES:
            digest.update(f.read(size))
        else:
            for offset in (0, size // 2 - SAMPLE_BYTES // 2, size - SAMPLE_BYTES):
                f.seek(offset)
                digest.update(f.read(SAMPLE_BYTES))
    return ID_PREFIX + digest.hexdigest()[:ID_HEX_DIGITS]


def iter_media_ids(media: MediaStore, start: str = "") -> Iterator[tuple[str, int]]:
    """Every media file under the folder `start` (default: the whole workspace) as (media id, depth),
    breadth first. Folders are visited once even through symlink loops."""
    queue: list[tuple[str, int]] = [(start, 0)]
    seen = {start}
    while queue:
        directory, depth = queue.pop(0)
        try:
            listing = media.list_dir(directory)
        except MediaError:
            continue
        for entry in listing.entries:
            if not entry.is_dir:
                yield entry.path, depth
                continue
            try:
                canonical = media.resolve(entry.path)
            except MediaError:
                continue
            if canonical not in seen and depth < MAX_SCAN_DEPTH:
                seen.add(canonical)
                queue.append((canonical, depth + 1))


class AssetBook:
    """The asset table of one workspace, loaded; `open_book` is how to get one."""

    def __init__(self, media: MediaStore) -> None:
        self.media = media
        self.dirty = False
        self._rows: dict[str, Asset] = {}
        self._digests: dict[tuple[str, int, float], str] = {}
        self._missing: set[str] = set()  # ids a scan already failed to find during this session
        payload = media.load_asset_table()
        rows = payload.get("assets") if isinstance(payload, dict) else None
        for asset_id, row in rows.items() if isinstance(rows, dict) else ():
            try:
                self._rows[str(asset_id)] = _asset_from_dict(str(asset_id), row)
            except (KeyError, TypeError, ValueError):
                continue  # a damaged row is rebuilt from the clip that needs it

    # --- reading ---------------------------------------------------------------

    def list(self) -> list[Asset]:
        return sorted(self._rows.values(), key=lambda a: (a.path, a.id))

    def get(self, asset_id: str) -> Asset | None:
        return self._rows.get(asset_id)

    def require(self, asset_id: str) -> Asset:
        """The row for `asset_id`; `AssetNotFoundError` if the table has none."""
        row = self._rows.get(asset_id)
        if row is None:
            raise AssetNotFoundError(f"unknown asset {asset_id!r}")
        return row

    def present(self, asset: Asset) -> bool:
        """Whether the asset's file is where the table says it is, with its content."""
        return self._valid(asset)

    def payload(self) -> dict[str, Any]:
        return {"version": TABLE_VERSION, "assets": {a.id: a.to_dict() for a in self.list()}}

    def save(self) -> None:
        if self.dirty:
            self.media.save_asset_table(self.payload())
            self.dirty = False

    # --- registering -----------------------------------------------------------

    def register(self, path: str) -> Asset:
        """The asset for the file at `path`, adding it to the table if it is new.

        Raises `MediaOutsideRootError` for a path outside the workspace, `SourceNotFoundError`
        if it isn't a file and `ProbeError` if it isn't readable media."""
        media_id = self.media.resolve(path)
        if not self.media.is_file(media_id):
            raise SourceNotFoundError(f"file not found: {path}")
        stat = self.media.stat(media_id)
        unchanged = self.unchanged(media_id, stat)
        if unchanged is not None:
            return unchanged  # unchanged since it was registered: nothing to hash
        return self._adopt(media_id, stat, self._digest(media_id, stat))

    def unchanged(self, media_id: str, stat: MediaStat) -> Asset | None:
        """The row for the file at `media_id` if its size and mtime are as recorded (as its path or a copy)."""
        for row in self._rows.values():
            known = (row.size, row.mtime) if row.path == media_id else row.copies.get(media_id)
            if known == (stat.size, stat.mtime):
                return row
        return None

    def __len__(self) -> int:
        return len(self._rows)

    def _digest(self, media_id: str, stat: MediaStat) -> str:
        key = (media_id, stat.size, stat.mtime)
        if key not in self._digests:
            self._digests[key] = sample_id(self.media, media_id, stat.size)
        return self._digests[key]

    def _adopt(self, media_id: str, stat: MediaStat, asset_id: str) -> Asset:
        """Record that the file at `media_id` (already hashed to `asset_id`) is that asset."""
        existing = self._rows.get(asset_id)
        if existing is not None and existing.path != media_id and self._valid(existing):
            # Another copy of a file we already have: the first path stays; remember where the copy is.
            existing = self._rows[asset_id]
            copy = (stat.size, stat.mtime)
            if existing.copies.get(media_id) != copy:
                existing = replace(existing, copies={**existing.copies, media_id: copy})
                self._rows[asset_id] = existing
                self.dirty = True
            return existing
        if existing is None:
            info = self.media.probe(media_id)
            row = Asset(
                asset_id,
                media_id,
                stat.size,
                stat.mtime,
                info.duration,
                info.has_audio,
                info.width,
                info.height,
                info.fps,
            )
        else:
            copies = {p: v for p, v in existing.copies.items() if p != media_id}
            row = replace(existing, path=media_id, size=stat.size, mtime=stat.mtime, copies=copies)
        if row != existing:
            self._rows[asset_id] = row
            self.dirty = True
        return row

    def _valid(self, row: Asset) -> bool:
        """Whether `row.path` still holds the asset. A file whose size and mtime are unchanged is
        trusted; otherwise the content is checked, and a touched-but-identical file keeps its row."""
        try:
            if not self.media.is_file(row.path):
                return False
            stat = self.media.stat(row.path)
        except MediaError:
            return False
        if (stat.size, stat.mtime) == (row.size, row.mtime):
            return True
        if stat.size != row.size or self._digest(row.path, stat) != row.id:
            return False
        self._rows[row.id] = replace(row, mtime=stat.mtime)
        self.dirty = True
        return True

    # --- finding files ----------------------------------------------------------

    def locate(self, asset_id: str, hint: str = "") -> Asset:
        """The asset's row with its file where the row says, relinking it if the file moved.

        `hint` is a clip's last known `source`, tried first when the table has no row for the id
        (a lost table). `AssetNotFoundError` if the id is unknown and there is no hint,
        `SourceNotFoundError` (naming the id and last known path) if the file can't be found."""
        row = self._rows.get(asset_id)
        if row is not None and self._valid(row):
            return self._rows[asset_id]
        found = None
        if asset_id not in self._missing:
            found = self._find(asset_id, None if row is None else row.size, hint, None if row is None else row.path)
        if found is None:
            self._missing.add(asset_id)
            if row is None and not hint:
                raise AssetNotFoundError(f"unknown asset {asset_id!r}")
            last = row.path if row is not None else hint
            raise SourceNotFoundError(
                f"source file not found: {last} (asset {asset_id}); restore the file or add it again"
            )
        media_id, stat = found
        return self._adopt(media_id, stat, asset_id)

    def where(self, asset_id: str, hint: str = "") -> Located:
        """`locate`, plus the path a clip whose `source` hint is `hint` should use."""
        row = self.locate(asset_id, hint)
        return Located(row, self.source_for(row, hint))

    def source_for(self, row: Asset, hint: str) -> str:
        """`hint` if it is the asset's path or a known copy still unchanged, else the asset's path."""
        if not hint or hint == row.path:
            return row.path
        copy = row.copies.get(hint)
        if copy is not None:
            try:
                stat = self.media.stat(hint)
                if (stat.size, stat.mtime) == copy:
                    return hint
            except MediaError:
                pass
            # The copy moved or changed: forget it.
            self._rows[row.id] = replace(row, copies={p: v for p, v in row.copies.items() if p != hint})
            self.dirty = True
        return row.path

    def _find(self, asset_id: str, size: int | None, hint: str, skip: str | None) -> tuple[str, MediaStat] | None:
        """A file in the workspace whose sampled hash is `asset_id`: the hint first, then a scan
        (only files of `size` when it is known) closest to the root first."""
        candidates: list[str] = []
        if hint:
            try:
                candidates.append(self.media.resolve(hint, strict=True))
            except MediaError:
                pass
        scanned = sorted(iter_media_ids(self.media), key=lambda item: (item[1], item[0].lower()))
        candidates += [media_id for media_id, _ in scanned]
        tried: set[str] = {skip} if skip else set()
        for media_id in candidates:
            if media_id in tried:
                continue
            tried.add(media_id)
            try:
                if not self.media.is_file(media_id):
                    continue
                stat = self.media.stat(media_id)
                if size is not None and stat.size != size:
                    continue
                if self._digest(media_id, stat) == asset_id:
                    return media_id, stat
            except (MediaError, OSError):
                continue
        return None


@contextmanager
def open_book(media: MediaStore) -> Iterator[AssetBook]:
    """The workspace's asset table, loaded under the process-wide lock and saved on exit if changed."""
    with _lock:
        book = AssetBook(media)
        try:
            yield book
        finally:
            book.save()


# --- conveniences for the front doors -----------------------------------------------


def list_assets(media: MediaStore) -> list[Asset]:
    with open_book(media) as book:
        return book.list()


def get_asset(media: MediaStore, asset_id: str) -> Asset:
    """The asset, relinked if its file moved. `AssetNotFoundError` for an unknown id."""
    with open_book(media) as book:
        book.require(asset_id)
        return book.locate(asset_id)


def register_path(media: MediaStore, path: str) -> Asset:
    with open_book(media) as book:
        return book.register(path)


def media_id_of(media: MediaStore, asset_id: str) -> str:
    """The current media id of an asset's file (the compose preview serves it)."""
    return get_asset(media, asset_id).path


# --- clips and projects ----------------------------------------------------------------


def lookup(book: AssetBook, asset_id: str, source: str) -> Located | None:
    """The asset and current path for a clip, leniently: None when it can't be found or read."""
    try:
        if asset_id:
            return book.where(asset_id, source)
        if source:
            row = book.register(source)
            return Located(row, book.source_for(row, book.media.resolve(source)))
    except (TimelineError, MediaError, ProbeError, OSError):
        pass
    return None


def peek(book: AssetBook, asset_id: str, source: str) -> Located | None:
    """Like `lookup`, but read-only: the table's row for the asset (or, with no asset id, for the file at
    `source`), never registering, relinking or re-hashing anything, so it never marks the table dirty. The
    path is the row's recorded one. Used where a dry run must not write (`edit_ops.py`)."""
    row = book.get(asset_id) if asset_id else None
    if row is None and not asset_id and source:
        row = next((r for r in book.list() if source == r.path or source in r.copies), None)
    return None if row is None else Located(row, row.path)


def link_project(media: MediaStore, project: Project) -> Project:
    """The project with every media clip linked to its asset, never raising for a missing file.

    Clips without an asset (version 1 files) get one when their source file exists; a clip whose
    file moved is relinked and its `source` hint refreshed; one whose file can't be found is kept
    as it is. Registers into the table (saved here), but writes no project: the service layer
    saves the result with the next edit, or `timeline_service.migrate_project` saves it now."""
    with open_book(media) as book:
        tracks = []
        for track in project.tracks:
            clips = []
            for clip in track.clips:
                if clip.kind == "media":
                    found = lookup(book, clip.asset, clip.source)
                    if found is not None:
                        clip = replace(clip, asset=found.asset.id, source=found.source)
                clips.append(clip)
            tracks.append(replace(track, clips=tuple(clips)))
        return replace(project, tracks=tuple(tracks))


def resolve_project(media: MediaStore, project: Project) -> Project:
    """Like `link_project` but strict, for rendering: every media clip's `source` becomes the file's
    current media id, and a clip whose file can't be found is an error."""
    with open_book(media) as book:
        tracks = []
        for track in project.tracks:
            clips = []
            for clip in track.clips:
                if clip.kind == "media":
                    if clip.asset:
                        found = book.where(clip.asset, clip.source)
                        clip = replace(clip, source=found.source)
                    else:
                        media_id = source_media_id(media, clip.source)
                        if not media.is_file(media_id):
                            raise SourceNotFoundError(f"source file not found: {clip.source}")
                        try:
                            row = book.register(media_id)
                            clip = replace(clip, asset=row.id, source=book.source_for(row, media_id))
                        except ProbeError:
                            clip = replace(clip, source=media_id)  # render's own probe reports it
                clips.append(clip)
            tracks.append(replace(track, clips=tuple(clips)))
        return replace(project, tracks=tuple(tracks))
