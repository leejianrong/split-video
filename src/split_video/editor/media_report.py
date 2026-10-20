"""HTTP routes for the media report of an asset (see `media_report.py`).

`GET /assets/{id}/report?bucket_seconds=&include=` returns every stored signal as one bounded, time-bucketed JSON
document and computes nothing. `POST /assets/{id}/report/prepare` queues an `analysis` job that makes the missing cheap
signals (loudness curve, silence scan, audio classification; never transcription, scenes or on-screen text); poll
`/jobs/{job_id}`. 404 for an unknown asset, 422 for a bad bucket size or `include`. The CLI (`report --json`) and MCP
(`media_report`) return the same JSON.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from split_video import assets, media_report
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.job_work import report_prepare_work
from split_video.jobs import ANALYSIS, JobQueue
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError


class ReportStartResponse(BaseModel):
    job_id: str


class ReportOut(BaseModel):
    """The media report (`media_report.media_report`, version 1). The nested parts are documented in
    docs/media-report.md; they are plain JSON so that the shape is identical on every surface."""

    version: int
    asset: dict[str, Any]
    signals: dict[str, Any]  # {available, missing, excluded}
    prepared: dict[str, Any] | None  # what `prepare` made; null for a read
    limits: dict[str, Any]  # {max_bytes}
    notices: dict[str, Any]  # {coarsened, shrunk, dropped, truncated}
    buckets: dict[str, Any] | None  # {seconds, count, <one array per column>}
    loudness: dict[str, Any] | None
    regions: dict[str, Any] | None
    silence_gaps: dict[str, Any] | None
    scene_changes: dict[str, Any] | None
    transcript: dict[str, Any] | None


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def asset_of(ctx: RequestContext, asset_id: str) -> assets.Asset:
        try:
            return assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.get("/assets/{asset_id}/report", response_model=ReportOut)
    def get_report(
        asset_id: str, bucket_seconds: float | None = None, include: str | None = None, ctx: RequestContext = ctx_dep
    ) -> dict[str, Any]:
        """Every stored signal of the asset in one document of at most 40 KB: asset info, which signals exist (and how
        to make the missing ones), whole-recording structure (loudness summary, audio class regions, silence gaps,
        strongest scene changes, transcript summary) and one array per column over time buckets (default
        max(10, duration / 180) seconds; `bucket_seconds` 5 to 600). `include` is a comma separated list of audio,
        loudness, silence, speech, scenes, ocr. Reads only: it never decodes or runs a model. When the document would
        be too large it is shrunk and `notices` says how. 404 for an unknown asset; 422 for a bad bucket size."""
        asset = asset_of(ctx, asset_id)
        try:
            return media_report.media_report(ctx.media, asset.id, bucket_seconds=bucket_seconds, include=include)
        except media_report.MediaReportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/assets/{asset_id}/report/prepare", response_model=ReportStartResponse)
    def prepare_report(asset_id: str, ctx: RequestContext = ctx_dep) -> ReportStartResponse:
        """Queue making the asset's missing cheap signals: the loudness curve, the silence scan and (when the YAMNet
        model is installed) the audio classification. Never transcribes, finds scenes or reads on-screen text. The job's
        result is `{asset, prepared, already, skipped}`; cancelling it stores no half-made signal. Then read the
        report with `GET /assets/{id}/report`."""
        asset = asset_of(ctx, asset_id)
        job_id = jobs.submit(
            ANALYSIS,
            report_prepare_work(ctx.media, asset.id),
            label=asset.path,
            total=media_report.PREPARE_STEPS,
            owner=ctx.principal.id,
        )
        return ReportStartResponse(job_id=job_id)

    return router
