"""Importing a file from outside the workspace: the rules for an upload.

A browser cannot hand the server a path, so a file dropped on the editor arrives as bytes. This module holds what
an import must satisfy and does it through the `MediaStore`: it picks a safe file name (no folders, a media
extension), chooses a free id in the folder, receives the bytes, probes the result and registers it as an asset.
The HTTP route (`editor/uploads.py`) only feeds it the request body. Nothing here builds a path: the store hands
out the real location for ffmpeg and for writing, and the id is opaque.
"""

from __future__ import annotations

import posixpath
from contextlib import ExitStack
from types import TracebackType
from typing import BinaryIO, Self

from split_video import assets
from split_video.ffmpeg import ProbeError
from split_video.media_store import MEDIA_EXTENSIONS, MediaNotFoundError, MediaStore

MAX_NAME_LENGTH = 180
# What the fix hint lists: the common ones, not all of `MEDIA_EXTENSIONS` (a long list is no help in a small card).
COMMON_TYPES = ".mp4, .mov, .mkv, .webm, .mp3, .wav, .m4a, .flac"


class ImportError_(ValueError):
    """An upload that cannot be accepted. `error_code` is the catalogue code (docs/errors.md)."""

    def __init__(self, message: str, error_code: str, **details: object) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details


def clean_name(name: str) -> str:
    """The file name to store: the last path component, no control characters, a known media extension."""
    base = posixpath.basename(name.replace("\\", "/")).strip()
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    if not base or base.startswith("."):
        raise ImportError_("the file needs a name", "upload.invalid_name")
    if len(base) > MAX_NAME_LENGTH:
        raise ImportError_(f"the file name is longer than {MAX_NAME_LENGTH} characters", "upload.invalid_name")
    extension = posixpath.splitext(base)[1].lower()
    if extension not in MEDIA_EXTENSIONS:
        raise ImportError_(
            f"{base!r} is not a supported media file ({extension or 'no extension'})",
            "upload.unsupported_type",
            extension=extension,
            supported=COMMON_TYPES,
        )
    return base


def free_id(media: MediaStore, folder: str, name: str) -> str:
    """The id of `name` in `folder`, or `name (2).ext`, `name (3).ext` ... when that file already exists."""
    parent = media.resolve(folder)
    if parent and (not media.exists(parent) or media.is_file(parent)):
        raise MediaNotFoundError(f"folder not found: {folder}")
    stem, extension = posixpath.splitext(name)
    candidate = media.join(parent, name)
    number = 2
    while media.exists(candidate):
        candidate = media.join(parent, f"{stem} ({number}){extension}")
        number += 1
    return candidate


class IncomingFile:
    """Receive one upload: `with IncomingFile(media, folder, name) as incoming: incoming.write(chunk); ...` then
    `incoming.finish()` for the asset. A failure (or leaving the block without `finish`) removes the partial file."""

    def __init__(self, media: MediaStore, folder: str, name: str) -> None:
        self._media = media
        self.media_id = free_id(media, folder, clean_name(name))
        self.size = 0
        self._stack = ExitStack()
        self._file: BinaryIO | None = None
        self._done = False

    def __enter__(self) -> Self:
        try:
            path = self._stack.enter_context(self._media.write_local(self.media_id))
            self._file = self._stack.enter_context(open(path, "wb"))
        except BaseException:
            self._stack.close()
            raise
        return self

    def write(self, chunk: bytes) -> None:
        assert self._file is not None
        self._file.write(chunk)
        self.size += len(chunk)

    def finish(self) -> assets.Asset:
        """Close the file and register it as an asset. An empty or unreadable file is removed and refused."""
        if self.size == 0:
            raise ImportError_("the upload is empty", "upload.empty")
        self._stack.close()
        self._done = True
        try:
            return assets.register_path(self._media, self.media_id)
        except ProbeError as exc:
            self._media.delete(self.media_id)
            raise ImportError_(str(exc), "media.unreadable") from None
        except BaseException:
            self._media.delete(self.media_id)
            raise

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if not self._done:
            self._stack.close()
            self._media.delete(self.media_id)
