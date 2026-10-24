"""HTTP route for a still image of an asset at a time (see `frames.py`).

`GET /assets/{id}/frame?time=&width=&height=&format=&source=&quality=` answers with the JPEG or PNG bytes (one fast
ffmpeg call, no job). What the frame is says in headers: `X-Frame-Time` (the frame actually returned),
`X-Frame-Width`, `X-Frame-Height`, `X-Frame-Source` ("proxy" or "original") and `X-Frame-Duration`. Errors are the
usual `{"detail": ...}`: 404 for an unknown asset, 400 for a bad format, source, quality or size, 422 for a
negative time, a time past the end (the message says how long the asset is) or an audio-only asset.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends
from starlette.responses import Response

from split_video import assets, frames
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError

# The same query on the same asset content gives the same bytes, except that a proxy made later changes what
# `source=auto` reads, so the cache is short and private.
CACHE_CONTROL = "private, max-age=300"

IMAGE_RESPONSES = {
    200: {
        "description": "The encoded image.",
        "content": {mime: {"schema": {"type": "string", "format": "binary"}} for mime in frames.FORMATS.values()},
    }
}


def make_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    @router.get("/assets/{asset_id}/frame", responses=IMAGE_RESPONSES, response_class=Response)
    def get_frame(
        asset_id: str,
        time: float,
        width: int | None = None,
        height: int | None = None,
        format: str = "jpeg",
        quality: int = frames.DEFAULT_QUALITY,
        source: str = "auto",
        ctx: RequestContext = ctx_dep,
    ) -> Response:
        """The asset's picture at `time` seconds as a JPEG (default) or PNG, optionally scaled (never up) to
        `width` and/or `height`, keeping the aspect ratio. `source=auto` reads the proxy when there is one (small
        and within 1/15 s of `time`); `source=original` reads the file itself."""
        try:
            asset = assets.get_asset(ctx.media, asset_id)
            frame = frames.extract_frame(
                ctx.media, asset.id, time, width=width, height=height, format=format, quality=quality, source=source
            )
        except (TimelineError, MediaError) as exc:
            raise api_error(404, exc) from None
        except frames.FrameArgumentError as exc:
            raise api_error(400, exc) from None
        except frames.FrameError as exc:
            raise api_error(422, exc) from None
        etag = hashlib.sha1(
            f"{asset.id}|{asset.size}|{asset.mtime}|{frame.source}|{time}|{width}|{height}|{frame.format}|{quality}".encode()
        ).hexdigest()
        return Response(
            frame.data,
            media_type=frame.mime,
            headers={
                "Cache-Control": CACHE_CONTROL,
                "ETag": f'"{etag}"',
                "X-Frame-Time": f"{frame.time:g}",
                "X-Frame-Width": str(frame.width),
                "X-Frame-Height": str(frame.height),
                "X-Frame-Source": frame.source,
                "X-Frame-Duration": f"{frame.duration:g}",
            },
        )

    return router
