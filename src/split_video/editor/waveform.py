"""HTTP routes for an asset's waveform (the timeline's audio lane; see `waveform.py` and `editor/cache.WaveformCache`).

`POST /assets/{id}/waveform` queues a `waveform` job (poll `/jobs/{job_id}`): one streamed decode of the audio, so a
three-hour recording never sits in memory, stored in the waveform cache next to the file. `GET /assets/{id}/waveform`
answers with what is stored and is a 404 until the job has run, like the scene signals and the proxy. The editor
draws from this and never decodes audio itself. An asset with no audio has no waveform: `POST` is a 422 and `GET` a 404
that says so.

The answer carries two curves; the finer one for the zoom level is the client's choice: `peaks` (min and max sample per
bucket, `duration / len(peaks)` seconds each, 2000 buckets) and `loudness` (RMS dB per `loudness_window` seconds,
1 s, floored at -100). A recording shorter than 2000 s has finer peaks, a longer one a finer loudness curve.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from split_video import assets
from split_video.editor.cache import WaveformCache
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.job_work import waveform_work
from split_video.jobs import WAVEFORM, JobQueue
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError

NO_AUDIO = "this asset has no audio, so it has no waveform"


class WaveformStartResponse(BaseModel):
    job_id: str


class AssetWaveformOut(BaseModel):
    asset: str
    duration: float  # seconds of audio the curves cover
    peaks: list[tuple[float, float]]  # (min, max) in -1..1 per bucket; `duration / len(peaks)` seconds each
    loudness_window: float | None  # seconds per loudness value (1.0); None if the cache has no curve
    loudness: list[float]  # RMS dB per window, -100 (digital silence) to 0


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def asset_of(ctx: RequestContext, asset_id: str) -> assets.Asset:
        try:
            return assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise api_error(404, exc) from None

    @router.post("/assets/{asset_id}/waveform", response_model=WaveformStartResponse)
    def start_waveform(asset_id: str, ctx: RequestContext = ctx_dep) -> WaveformStartResponse:
        """Queue making the asset's waveform (peaks and loudness). The job's result is
        `{asset, buckets, loudness_windows}`; read it with `GET /assets/{id}/waveform`. 422 for an asset without audio."""
        asset = asset_of(ctx, asset_id)
        if not asset.has_audio:
            raise HTTPException(status_code=422, detail=NO_AUDIO)
        job_id = jobs.submit(
            WAVEFORM, waveform_work(ctx.media, asset.id), label=asset.path, total=1, owner=ctx.principal.id
        )
        return WaveformStartResponse(job_id=job_id)

    @router.get("/assets/{asset_id}/waveform", response_model=AssetWaveformOut)
    def get_waveform(asset_id: str, ctx: RequestContext = ctx_dep) -> AssetWaveformOut:
        """The stored waveform of the asset: peaks and the loudness curve (see the module description). Never
        decodes audio. 404 until a `waveform` job has run (or after the file changed), and for an asset without audio."""
        asset = asset_of(ctx, asset_id)
        if not asset.has_audio:
            raise HTTPException(status_code=404, detail=NO_AUDIO)
        cache = WaveformCache(ctx.media, asset.path)
        peaks, loudness = cache.stored_peaks(), cache.stored_loudness()
        if peaks is None or loudness is None:
            raise HTTPException(status_code=404, detail="no waveform yet: POST this path to make one")
        return AssetWaveformOut(
            asset=asset.id,
            duration=asset.duration,
            peaks=[(round(lo, 3), round(hi, 3)) for lo, hi in peaks.buckets],
            loudness_window=loudness.window_seconds,
            loudness=[round(v, 1) for v in loudness.db],
        )

    return router
