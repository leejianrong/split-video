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

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from split_video import assets, ocr, proxy, scenes, transcribe
from split_video.error_catalogue import REGISTRY, describe
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
    # [{"path", "reason", "code", "fix"}]: a short reason without absolute paths, the error catalogue's code and fix
    skipped: list[dict[str, str]] = field(default_factory=list)

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


_ABSOLUTE_DIRS = re.compile(r"(?<![\w.])/(?:[^\s/:'\"]+/)+")
_BRACKET_PREFIX = re.compile(r"\[[^\]]*@ 0x[0-9a-f]+\]\s*")
MAX_REASON = 200


def short_reason(exc: BaseException, path: str) -> str:
    """One line saying why `exc` made the scan skip `path`: ffprobe's last line (its first lines repeat the absolute
    file name and its internal addresses), with every absolute directory removed."""
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()] or [type(exc).__name__]
    line = lines[-1]
    name = posixpath.basename(path)
    cut = line.rfind(f"{name}: ")
    if cut >= 0:
        line = line[cut + len(name) + 2 :]
    line = _ABSOLUTE_DIRS.sub("", _BRACKET_PREFIX.sub("", line))
    if isinstance(exc, ProbeError):
        line = f"could not read as media: {line}"
    return line[:MAX_REASON]


def skip_entry(path: str, exc: BaseException) -> dict[str, str]:
    """The `skipped` row for a file that could not be registered (see `ScanReport`)."""
    if isinstance(exc, (ProbeError, OSError)):
        return {
            "path": path,
            "reason": short_reason(exc, path),
            "code": "media.unreadable",
            "fix": REGISTRY["media.unreadable"].fix,
        }
    info = describe(exc)
    return {"path": path, "reason": short_reason(exc, path), "code": info.code, "fix": info.fix}


def _scan_one(media: MediaStore, book: assets.AssetBook, path: str, report: ScanReport) -> None:
    try:
        stat = media.stat(path)
        if book.unchanged(path, stat) is not None:
            report.unchanged += 1
            return
        before = len(book)
        row = book.register(path)
    except (TimelineError, MediaError, ProbeError, OSError) as exc:
        report.skipped.append(skip_entry(path, exc))
        return
    if len(book) > before:
        report.added.append(row)
    else:
        report.copies.append(path)


def listing(media: MediaStore) -> list[dict[str, Any]]:
    """The library as the CLI, API and MCP show it: every asset's summary (see `Asset.summary`, plus `proxy` and
    `thumbnails`: whether current ones exist, see `proxy.py`, `transcript`, see `transcribe.py`, `scenes`, see `scenes.py`, and `ocr`, see `ocr.py`).
    Reads the table only; nothing is probed."""
    with assets.open_book(media) as book:
        return [
            {
                **a.summary(book.present(a)),
                **proxy.flags(media, a),
                "transcript": transcribe.has_transcript(media, a),
                "scenes": scenes.has_scenes(media, a),
                "ocr": ocr.has_ocr(media, a),
            }
            for a in book.list()
        ]
