"""HTTP routes for an asset's proxy and thumbnail strip (see `proxy.py`).

`POST /assets/{id}/proxy` queues a `proxy` job (poll `/jobs/{job_id}`; progress is thousandths);
`GET /assets/{id}/proxy` serves the proxy MP4 with Range support, so a `<video>` seeks in it;
`GET /assets/{id}/thumbnails` serves the sprite JPEG and `.../thumbnails/info` says how to cut it.

An audio-only asset has neither. Asking the API to make one (`POST`) is an impossible request: 422. Asking for
one (`GET`) is a 404 whose message says audio-only assets have none (rather than "POST to make one", which
would only get a 422). The CLI (`library proxies`) and MCP (`prepare_asset`) are not HTTP requests: they
report such an asset as a result with `skipped: "audio-only asset"` and a null `proxy` and `thumbnails`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import Response

from split_video import assets, proxy
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.media_response import media_response
from split_video.job_work import proxy_work
from split_video.jobs import PROXY, JobQueue
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError


class ProxyRequest(BaseModel):
    force: bool = False  # rebuild even if a current proxy and strip exist


class ProxyStartResponse(BaseModel):
    job_id: str


class ThumbnailsInfo(BaseModel):
    """How to cut the sprite: the tile for time T is `index = min(count - 1, int(T / interval))`, at column
    `index % columns`, row `index // columns`, of `tile_width` x `tile_height` px each."""

    tile_width: int
    tile_height: int
    columns: int
    rows: int
    count: int
    interval: float  # seconds of video per tile
    width: int  # the sprite image's size
    height: int
    duration: float
    source: str  # "proxy" or "original": what the frames were taken from
    method: str  # "keyframes" (keyframes only, fast) or "seek" (one exact frame per tile)
    tolerance: float  # seconds a tile's frame may be from `index * interval`


AUDIO_ONLY = "audio-only asset: no proxy or thumbnails"


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def asset_of(ctx: RequestContext, asset_id: str) -> assets.Asset:
        try:
            return assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post("/assets/{asset_id}/proxy", response_model=ProxyStartResponse)
    def make_proxy(
        asset_id: str, request: ProxyRequest | None = None, ctx: RequestContext = ctx_dep
    ) -> ProxyStartResponse:
        """Queue making the asset's proxy and thumbnail strip (whichever is missing; both with `force`). The job's
        result is `{asset, proxy, thumbnails, made}`. An audio-only asset has neither: 422 (the CLI and
        MCP report it as `skipped` instead)."""
        asset = asset_of(ctx, asset_id)
        if not asset.has_video:
            raise HTTPException(status_code=422, detail=AUDIO_ONLY)
        job_id = jobs.submit(
            PROXY,
            proxy_work(ctx.media, asset.id, force=bool(request and request.force)),
            label=asset.path,
            total=proxy.STEPS,
            owner=ctx.principal.id,
        )
        return ProxyStartResponse(job_id=job_id)

    @router.get("/assets/{asset_id}/proxy")
    def get_proxy(asset_id: str, request: Request, ctx: RequestContext = ctx_dep) -> Response:
        """The proxy video (H.264 MP4, Range requests answered with 206). 404 until the proxy job has run, and
        for ever for an audio-only asset (the message says so)."""
        asset = asset_of(ctx, asset_id)
        if not asset.has_video:
            raise HTTPException(status_code=404, detail=AUDIO_ONLY)
        if proxy.proxy_info(ctx.media, asset) is None:
            raise HTTPException(status_code=404, detail="no proxy yet: POST this path to make one")
        return media_response(ctx.media, proxy.proxy_id(ctx.media, asset.path), request)

    @router.get("/assets/{asset_id}/thumbnails")
    def get_thumbnails(asset_id: str, request: Request, ctx: RequestContext = ctx_dep) -> Response:
        """The thumbnail strip: one JPEG sprite of evenly spaced frames (see `/thumbnails/info`). 404 until the
        proxy job has run, and for an audio-only asset (the message says so)."""
        asset = asset_of(ctx, asset_id)
        if not asset.has_video:
            raise HTTPException(status_code=404, detail=AUDIO_ONLY)
        if proxy.thumbnails_info(ctx.media, asset) is None:
            raise HTTPException(status_code=404, detail="no thumbnails yet: POST /assets/{id}/proxy to make them")
        return media_response(ctx.media, proxy.thumbnails_id(ctx.media, asset.path), request)

    @router.get("/assets/{asset_id}/thumbnails/info", response_model=ThumbnailsInfo)
    def get_thumbnails_info(asset_id: str, ctx: RequestContext = ctx_dep) -> ThumbnailsInfo:
        """The sprite's layout: tile size, columns, rows, tile count and seconds per tile (and `tolerance`, how
        far a tile's frame may be from its nominal time). 404 as `GET .../thumbnails`."""
        asset = asset_of(ctx, asset_id)
        if not asset.has_video:
            raise HTTPException(status_code=404, detail=AUDIO_ONLY)
        info = proxy.thumbnails_info(ctx.media, asset)
        if info is None:
            raise HTTPException(status_code=404, detail="no thumbnails yet: POST /assets/{id}/proxy to make them")
        return ThumbnailsInfo(**info)

    return router
