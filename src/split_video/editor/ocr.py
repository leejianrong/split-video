"""HTTP routes for an asset's on-screen text (see `ocr.py`).

`POST /assets/{id}/ocr` queues an `ocr` job (poll `/jobs/{job_id}`; progress counts frames), `GET /assets/{id}/ocr`
returns the stored text a page at a time (`start`, `end`, `limit`, `cursor`) and `GET /assets/{id}/ocr/search?q=`
finds text on screen. Without the `tesseract` program the POST answers 501 with `ocr.UNAVAILABLE` (the install hint)
as its `detail`, before queueing anything; an audio-only asset is a 422. Reads answer 404 until the text has been
made and 422 for a bad range, limit, cursor or query. The CLI (`ocr`, exit code 4) and MCP (`ocr_asset`, a tool
error) report the same text.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from split_video import assets, ocr
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.job_work import ocr_work
from split_video.jobs import OCR, JobQueue
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError

AUDIO_ONLY = "audio-only asset: no picture to read"


class OcrRequest(BaseModel):
    interval: float = Field(ocr.DEFAULT_INTERVAL, ge=ocr.MIN_INTERVAL)  # read a frame every this many seconds
    max_frames: int = Field(ocr.DEFAULT_MAX_FRAMES, ge=1, le=ocr.MAX_FRAMES_LIMIT)  # cap on frames read
    force: bool = False  # redo even if current text made with these parameters exists


class OcrStartResponse(BaseModel):
    job_id: str


class OcrRangeBoundsOut(BaseModel):
    start: float
    end: float | None  # null: to the end


class OcrStateOut(BaseModel):
    time: float  # seconds: the first sampled frame showing this text
    until: float  # seconds: when other text replaced it (the duration for the last)
    text: str
    lines: list[str]
    source_scene: bool  # the frame was chosen because of a scene change


class OcrPageOut(BaseModel):
    """One page of the stored text (`ocr.ocr_range`)."""

    asset: str
    engine: str
    duration: float
    range: OcrRangeBoundsOut
    total_in_range: int
    count: int
    items: list[OcrStateOut]
    next_cursor: str | None  # pass back as `cursor` for the next page; null on the last


class OcrHitOut(BaseModel):
    source: str  # always "ocr": the same field marks a transcript hit in a combined search
    time: float
    end: float
    score: float  # 0..1
    text: str  # the line that matched best
    context_before: str
    context_after: str
    matched_tokens: list[str]
    frame: int  # index in the stored document's frames


class OcrSearchOut(BaseModel):
    """One page of hits for some text (`ocr.ocr_search`)."""

    asset: str
    query: str
    tokens: list[str]
    range: OcrRangeBoundsOut
    total_matches: int
    count: int
    hits: list[OcrHitOut]
    next_cursor: str | None


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def asset_of(ctx: RequestContext, asset_id: str) -> assets.Asset:
        try:
            return assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post("/assets/{asset_id}/ocr", response_model=OcrStartResponse)
    def start_ocr(asset_id: str, request: OcrRequest | None = None, ctx: RequestContext = ctx_dep) -> OcrStartResponse:
        """Queue reading the text on the asset's screen: a frame at each scene change (when scenes exist) and every
        `interval` seconds, read from the original with Tesseract. The job's result is `{asset, engine, duration,
        interval, max_frames, scenes_used, frames, scene_frames, ocr_frames, skipped_unchanged, capped,
        frames_before_cap, failed_frames, note, made}`; read the text with `GET /assets/{id}/ocr` and search it with
        `GET /assets/{id}/ocr/search`. 501 (detail: the install hint) without the tesseract program; 422 for an
        audio-only asset."""
        request = request or OcrRequest()
        asset = asset_of(ctx, asset_id)
        if not asset.has_video:
            raise HTTPException(status_code=422, detail=AUDIO_ONLY)
        try:
            ocr.require_available()
        except ocr.OcrUnavailableError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from None
        job_id = jobs.submit(
            OCR,
            ocr_work(
                ctx.media, asset.id, interval=request.interval, max_frames=request.max_frames, force=request.force
            ),
            label=asset.path,
            owner=ctx.principal.id,
        )
        return OcrStartResponse(job_id=job_id)

    def read(call, asset_id: str, ctx: RequestContext, *args, **kwargs):
        asset = asset_of(ctx, asset_id)
        try:
            return call(ctx.media, asset.id, *args, **kwargs)
        except ocr.OcrNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ocr.OcrArgumentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/assets/{asset_id}/ocr", response_model=OcrPageOut)
    def get_ocr(
        asset_id: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        ctx: RequestContext = ctx_dep,
    ) -> OcrPageOut:
        """The stored on-screen text that was up at some point between `start` and `end` seconds (the whole
        recording by default), one page of `limit` states (default 20, at most 200) with a `next_cursor` to pass back
        until it is null. Each item is `{time, until, text, lines, source_scene}`. 404 until an ocr job has run, or
        after the file changed; 422 for a bad span, limit or cursor."""
        return OcrPageOut(**read(ocr.ocr_range, asset_id, ctx, start, end, limit=limit, cursor=cursor))

    @router.get("/assets/{asset_id}/ocr/search", response_model=OcrSearchOut)
    def search_ocr(
        asset_id: str,
        q: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        context: int | None = None,
        ctx: RequestContext = ctx_dep,
    ) -> OcrSearchOut:
        """Where the text `q` was on screen: fuzzy and token-based (case-insensitive, split on anything that is not a
        letter or digit, so `test_cut_burst` finds `test cut burst`; every token must be in the same frame). Hits best
        first with `source` ("ocr"), `time`, `end`, `score` (0..1), `text` (the best line), `context_before` /
        `context_after` (`context` lines each, default 1, at most 10) and `matched_tokens`; `limit` per page (default
        20, at most 200) and a `next_cursor`. `start` / `end` keep text that was up in that span. 404 without stored
        text; 422 for an empty or over-long query (500 characters), a bad span, limit or cursor."""
        return OcrSearchOut(
            **read(ocr.ocr_search, asset_id, ctx, q, start=start, end=end, limit=limit, cursor=cursor, context=context)
        )

    return router
