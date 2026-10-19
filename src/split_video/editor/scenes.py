"""HTTP routes for an asset's scene changes and activity curve (see `scenes.py`).

`POST /assets/{id}/scenes` queues a `scene_analysis` job (poll `/jobs/{job_id}`; progress is thousandths of the
video) and `GET /assets/{id}/scenes` returns the stored signals (404 if there are none yet), optionally cut to
`start` / `end` seconds. An audio-only asset has no picture: the POST answers 422, as `POST .../proxy` does.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from split_video import assets, scenes
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.job_work import SCENE_STEPS, scene_work
from split_video.jobs import SCENE_ANALYSIS, JobQueue
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError

AUDIO_ONLY = "audio-only asset: no picture to analyse"


class ScenesRequest(BaseModel):
    threshold: float = Field(scenes.DEFAULT_THRESHOLD, ge=0, le=1)  # a cut needs a score of at least this (0..1)
    min_gap: float = Field(scenes.DEFAULT_MIN_GAP, ge=0)  # cuts closer than this many seconds count as one
    force: bool = False  # redo even if current signals made with these parameters exist


class ScenesStartResponse(BaseModel):
    job_id: str


class SceneOut(BaseModel):
    time: float  # seconds from the start of the asset: the first frame of the new shot
    score: float  # 0..1, how abrupt the change was


class ScenesOut(BaseModel):
    """The stored signals (the document described in `scenes.py`)."""

    version: int
    asset: str
    source: str  # "proxy" or "original": what was analysed
    duration: float
    threshold: float
    min_gap: float
    sample_fps: float
    activity_interval: float  # seconds per activity value
    scenes: list[SceneOut]
    activity: list[float]  # 0..1 per interval; activity[k] covers [k * interval, (k + 1) * interval)
    activity_start: float | None = None  # with start/end: the second the first activity value covers


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def asset_of(ctx: RequestContext, asset_id: str) -> assets.Asset:
        try:
            return assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post("/assets/{asset_id}/scenes", response_model=ScenesStartResponse)
    def start_scene_analysis(
        asset_id: str, request: ScenesRequest | None = None, ctx: RequestContext = ctx_dep
    ) -> ScenesStartResponse:
        """Queue finding the asset's scene changes and screen-activity curve, from its proxy if it has a current one
        (else the original). The job's result is `{asset, source, duration, threshold, min_gap, scenes,
        activity_buckets, made}`; read the signals with `GET /assets/{id}/scenes`. 422 for an audio-only asset."""
        request = request or ScenesRequest()
        asset = asset_of(ctx, asset_id)
        if not asset.has_video:
            raise HTTPException(status_code=422, detail=AUDIO_ONLY)
        job_id = jobs.submit(
            SCENE_ANALYSIS,
            scene_work(ctx.media, asset.id, threshold=request.threshold, min_gap=request.min_gap, force=request.force),
            label=asset.path,
            total=SCENE_STEPS,
            owner=ctx.principal.id,
        )
        return ScenesStartResponse(job_id=job_id)

    @router.get("/assets/{asset_id}/scenes", response_model=ScenesOut, response_model_exclude_none=True)
    def get_scenes(
        asset_id: str, start: float | None = None, end: float | None = None, ctx: RequestContext = ctx_dep
    ) -> ScenesOut:
        """The stored signals, or with `start` and/or `end` (seconds) only the cuts in that span and the activity
        values overlapping it (`activity_start` says which second the first one covers). 404 until a
        scene_analysis job has run, or after the file changed."""
        asset = asset_of(ctx, asset_id)
        found = scenes.load(ctx.media, asset.id)
        if found is None:
            raise HTTPException(status_code=404, detail="no scene signals yet: POST /assets/{id}/scenes to make them")
        return ScenesOut(**scenes.excerpt(found, start, end))

    return router
