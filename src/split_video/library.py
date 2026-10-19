"""The workspace library: scan a folder and record every media file as an asset.

`scan` walks a folder of the workspace (recursively; hidden entries, the cache directory and
non-media extensions are left out by `MediaStore.list_dir`) and registers each file in the asset
table (see `assets.py`). A row holds what the one probe learned: duration, size, whether there is
video and audio, width, height and fps. Listing the library reads the table and never probes.

A scan is incremental. A file whose size and mtime match its row is skipped without opening it;
a new file costs one sampled hash (at most 3 MiB read, however long the recording is) and one
ffprobe, which reads the container headers rather than the media; the same bytes at a second
path are recorded as a copy and not probed again. A file that can't be read as media is reported
as skipped with the reason and left out of the table (so the next scan tries it again); it never
stops the scan.

The table is saved every `SAVE_EVERY` files, so a cancelled or crashed scan keeps what it found,
and the process-wide table lock is not held for the whole of a long scan.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from split_video import assets, proxy, transcribe
from split_video.ffmpeg import ProbeError
from split_video.jobs import CancelToken
from split_video.media_store import MediaError, MediaStore
from split_video.timeline import TimelineError

SAVE_EVERY = 25

ProgressFn = Callable[[int, int, str], None]  # (files done, files total, the file just handled)


@dataclass
class ScanReport:
    folder: str
    total: int = 0  # media files found
    added: list[assets.Asset] = field(default_factory=list)  # new assets, in the order found
    # Paths whose bytes were already in the library: a second copy of a file, or a file that moved.
    copies: list[str] = field(default_factory=list)
    unchanged: int = 0  # files already recorded, not re-probed
    skipped: list[dict[str, str]] = field(default_factory=list)  # [{"path", "reason"}]

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "total": self.total,
            "added": [{"id": a.id, "path": a.path} for a in self.added],
            "copies": list(self.copies),
            "unchanged": self.unchanged,
            "skipped": list(self.skipped),
        }


def scan(
    media: MediaStore,
    folder: str = "",
    *,
    on_progress: ProgressFn | None = None,
    cancel: CancelToken | None = None,
) -> ScanReport:
    """Register every media file under `folder` (default: the whole workspace).

    Raises `MediaNotFoundError` if `folder` isn't a directory and `MediaOutsideRootError` if it
    escapes the workspace; `JobCancelledError` through `cancel`. Per-file problems are in `skipped`."""
    start = media.resolve(folder)
    media.list_dir(start)  # fail up front for a bad folder, instead of reporting an empty library
    paths = sorted((p for p, _ in assets.iter_media_ids(media, start)), key=str.lower)
    report = ScanReport(folder=start, total=len(paths))
    for batch_start in range(0, len(paths), SAVE_EVERY):
        with assets.open_book(media) as book:
            for index in range(batch_start, min(batch_start + SAVE_EVERY, len(paths))):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                _scan_one(media, book, paths[index], report)
                if on_progress is not None:
                    on_progress(index + 1, len(paths), paths[index])
    return report


def _scan_one(media: MediaStore, book: assets.AssetBook, path: str, report: ScanReport) -> None:
    try:
        stat = media.stat(path)
        if book.unchanged(path, stat) is not None:
            report.unchanged += 1
            return
        before = len(book)
        row = book.register(path)
    except (TimelineError, MediaError, ProbeError, OSError) as exc:
        report.skipped.append({"path": path, "reason": str(exc) or type(exc).__name__})
        return
    if len(book) > before:
        report.added.append(row)
    else:
        report.copies.append(path)


def listing(media: MediaStore) -> list[dict[str, Any]]:
    """The library as the CLI, API and MCP show it: every asset's summary (see `Asset.summary`, plus `proxy` and
    `thumbnails`: whether current ones exist, see `proxy.py`, and `transcript`, see `transcribe.py`).
    Reads the table only; nothing is probed."""
    with assets.open_book(media) as book:
        return [
            {**a.summary(book.present(a)), **proxy.flags(media, a), "transcript": transcribe.has_transcript(media, a)}
            for a in book.list()
        ]
