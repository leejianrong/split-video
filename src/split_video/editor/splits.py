"""HTTP route for splitting a recording at its silences, run as a `split` job.

`POST /assets/{id}/split` queues the engine's `project.split_by_silence`: the silent gaps are found (one ffmpeg pass,
cached next to the video, so a repeat is instant), turned into segments and saved as the video's segments, replacing
the saved ones. This is what the editor's "Split by silence" and "Find songs" do; the only difference is
`label_prefix` ("Song" labels the cuts "Song 1", "Song 2"). The job's progress counts thousandths of the audio and its
result is `{video, segments, revision, labels}`; follow it through `/jobs/{id}` or `/events`, cancel it with
`POST /jobs/{id}/cancel` (nothing is saved). `If-Match` makes the save fail with a revision conflict, as a job error, if
the project changed since. The CLI (`propose --apply`) and MCP (`propose_splits`) run the same operation inline.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from split_video import assets
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.editor.revisions import WRITE_RESPONSES, base_revision
from split_video.job_work import split_work
from split_video.jobs import SPLIT, JobQueue
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError

AUDIO_MISSING = "the asset has no audio track, so there is no silence to split at"


class SplitRequest(BaseModel):
    silence_threshold: float = Field(-35.0, description="Quieter than this many dB counts as silence.")
    min_silence_duration: float = Field(2.0, description="A gap must be at least this many seconds long to cut at.")
    min_song_length: float = Field(30.0, description="Pieces shorter than this many seconds are merged away.")
    padding: float = Field(0.15, description="Seconds kept on each side of a piece.")
    label_prefix: str | None = Field(None, description='Label the pieces "<prefix> 1", "<prefix> 2" ... (e.g. "Song").')


class SplitStartResponse(BaseModel):
    job_id: str


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    @router.post("/assets/{asset_id}/split", response_model=SplitStartResponse, responses=WRITE_RESPONSES)
    def start_split(
        asset_id: str,
        request: SplitRequest | None = None,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> SplitStartResponse:
        """Queue splitting the asset's recording at its silences and saving the segments as its project (a `split`
        job, result `{video, segments, revision, labels}`). 404 for an unknown asset, 422 for an asset without audio."""
        request = request or SplitRequest()
        try:
            asset = assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise api_error(404, exc) from None
        if not asset.has_audio:
            raise HTTPException(status_code=422, detail=AUDIO_MISSING)
        job_id = jobs.submit(
            SPLIT,
            split_work(
                ctx.media,
                ctx.store,
                asset.path,
                silence_threshold=request.silence_threshold,
                min_silence_duration=request.min_silence_duration,
                min_song_length=request.min_song_length,
                padding=request.padding,
                label_prefix=request.label_prefix,
                base_revision=base,
            ),
            label=asset.path,
            owner=ctx.principal.id,
        )
        return SplitStartResponse(job_id=job_id)

    return router
