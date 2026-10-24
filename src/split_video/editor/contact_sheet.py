"""HTTP routes for a contact sheet of an asset (see `contact_sheet.py`).

`GET /assets/{id}/contact-sheet?start=&end=&count=&columns=&tile_width=&format=&source=` answers with the JPEG or
PNG grid (ffmpeg calls bounded by `count`, no job). The headers say what is in it: `X-Sheet-Columns`,
`X-Sheet-Rows`, `X-Sheet-Tile-Width`, `X-Sheet-Tile-Height`, `X-Sheet-Width`, `X-Sheet-Height`, `X-Sheet-Source`
("proxy" or "original"), `X-Sheet-Duration`, `X-Sheet-Start`, `X-Sheet-End` and `X-Sheet-Times` (the tile times in
seconds, comma separated, row-major: tile i is at column i % columns, row i // columns).
`GET /assets/{id}/contact-sheet/info` takes the same query (except `format`) and returns the same map as JSON,
with each tile's `time`, `x` and `y`, without rendering anything. Errors are `{"detail": ...}` as for frames: 404
unknown asset, 400 a bad count, columns, tile_width, format, source or a too-wide image, 422 a bad range (the
message says how long the asset is), an audio-only asset or a missing font.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import Response

from split_video import assets, contact_sheet
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.editor.frames import CACHE_CONTROL, IMAGE_RESPONSES
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, contact_sheet.FrameArgumentError):
        return api_error(400, exc)
    if isinstance(exc, contact_sheet.FrameError):
        return api_error(422, exc)
    return api_error(404, exc)


def make_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    @router.get("/assets/{asset_id}/contact-sheet", responses=IMAGE_RESPONSES, response_class=Response)
    def get_contact_sheet(
        asset_id: str,
        start: float = 0.0,
        end: float | None = None,
        count: int = contact_sheet.DEFAULT_COUNT,
        columns: int | None = None,
        tile_width: int = contact_sheet.DEFAULT_TILE_WIDTH,
        format: str = "jpeg",
        source: str = "auto",
        ctx: RequestContext = ctx_dep,
    ) -> Response:
        """One image: `count` (1..64, default 12) frames evenly spread over `[start, end)` seconds (the centre
        of each equal sub-interval; `end` defaults to the asset's end), in a grid of `columns` (default
        near-square, wider than tall) tiles `tile_width` px wide, each stamped with its time. Row-major; the
        `X-Sheet-Times` header lists the times."""
        try:
            asset = assets.get_asset(ctx.media, asset_id)
            sheet = contact_sheet.make_sheet(
                ctx.media,
                asset.id,
                start,
                end,
                count=count,
                columns=columns,
                tile_width=tile_width,
                format=format,
                source=source,
            )
        except (TimelineError, MediaError, contact_sheet.FrameError) as exc:
            raise _fail(exc) from None
        lay = sheet.layout
        etag = hashlib.sha1(
            f"{asset.id}|{asset.size}|{asset.mtime}|{lay.source}|{start}|{end}|{count}|{columns}|{lay.tile_width}|{sheet.format}".encode()
        ).hexdigest()
        return Response(
            sheet.data,
            media_type=sheet.mime,
            headers={
                "Cache-Control": CACHE_CONTROL,
                "ETag": f'"{etag}"',
                "X-Sheet-Columns": str(lay.columns),
                "X-Sheet-Rows": str(lay.rows),
                "X-Sheet-Tile-Width": str(lay.tile_width),
                "X-Sheet-Tile-Height": str(lay.tile_height),
                "X-Sheet-Width": str(lay.width),
                "X-Sheet-Height": str(lay.height),
                "X-Sheet-Source": lay.source,
                "X-Sheet-Duration": f"{lay.duration:g}",
                "X-Sheet-Start": f"{lay.start:g}",
                "X-Sheet-End": f"{lay.end:g}",
                "X-Sheet-Times": ",".join(f"{t.time:g}" for t in lay.tiles),
            },
        )

    @router.get("/assets/{asset_id}/contact-sheet/info")
    def get_contact_sheet_info(
        asset_id: str,
        start: float = 0.0,
        end: float | None = None,
        count: int = contact_sheet.DEFAULT_COUNT,
        columns: int | None = None,
        tile_width: int = contact_sheet.DEFAULT_TILE_WIDTH,
        source: str = "auto",
        ctx: RequestContext = ctx_dep,
    ) -> dict[str, Any]:
        """The tile map of the contact sheet with the same query (time, column/row position in pixels `x`,
        `y` of every tile, grid and image size), without rendering it."""
        try:
            return contact_sheet.plan_sheet(
                ctx.media, asset_id, start, end, count=count, columns=columns, tile_width=tile_width, source=source
            ).info()
        except (TimelineError, MediaError, contact_sheet.FrameError) as exc:
            raise _fail(exc) from None

    return router
