"""Serve a stored media file over HTTP with Range support.

The `<video>` element seeks by sending `Range: bytes=a-b` and expects a 206
back, so this answers single byte ranges (multi-range requests get the whole
file, which the spec allows). It reads through `MediaStore.open_read`, so a
store that isn't a local disk serves the same way.
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Iterator
from email.utils import formatdate

from fastapi import HTTPException, Request
from starlette.responses import Response, StreamingResponse

from split_video.media_store import MediaNotFoundError, MediaStore

CHUNK = 64 * 1024
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The inclusive (start, end) a Range header asks for; None for no (usable)
    range; raises ValueError if it can't be satisfied."""
    if not header:
        return None
    match = _RANGE.match(header.strip())
    if match is None:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":  # the final N bytes
        length = int(last)
        if length == 0:
            raise ValueError("unsatisfiable")
        return max(0, size - length), size - 1
    start = int(first)
    end = size - 1 if last == "" else min(int(last), size - 1)
    if start >= size or end < start:
        raise ValueError("unsatisfiable")
    return start, end


def _stream(media: MediaStore, media_id: str, start: int, end: int) -> Iterator[bytes]:
    with media.open_read(media_id) as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def media_response(media: MediaStore, media_id: str, request: Request) -> Response:
    try:
        stat = media.stat(media_id)
    except MediaNotFoundError:
        raise HTTPException(status_code=404, detail="file not found") from None
    size = stat.size
    headers = {"Accept-Ranges": "bytes", "Last-Modified": formatdate(stat.mtime, usegmt=True)}
    content_type = mimetypes.guess_type(media.name(media_id))[0] or "application/octet-stream"
    try:
        byte_range = _parse_range(request.headers.get("range"), size)
    except ValueError:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    if size == 0:
        return Response(b"", media_type=content_type, headers=headers)
    if byte_range is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(_stream(media, media_id, 0, size - 1), media_type=content_type, headers=headers)
    start, end = byte_range
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(
        _stream(media, media_id, start, end), status_code=206, media_type=content_type, headers=headers
    )
