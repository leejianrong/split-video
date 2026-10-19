"""HTTP routes for an asset's transcript (see `transcribe.py`).

`POST /assets/{id}/transcribe` queues a `transcription` job (poll `/jobs/{job_id}`; progress is thousandths of
the audio) and `GET /assets/{id}/transcript` returns the stored transcript (404 if there is none yet).

Without the optional faster-whisper package the POST answers 501 with `transcribe.UNAVAILABLE` (the install
hint) as its `detail`, before queuing anything; an asset with no audio is a 422. The CLI (`transcribe`, exit
code 4) and MCP (`transcribe_asset`, a tool error) report the same text.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from split_video import assets, transcribe
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.job_work import TRANSCRIBE_STEPS, transcribe_work
from split_video.jobs import TRANSCRIPTION, JobQueue
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError


class TranscribeRequest(BaseModel):
    model: str = transcribe.DEFAULT_MODEL  # a faster-whisper model name: base.en, small.en, base, small, ...
    language: str | None = None  # e.g. "en"; unset detects it (a `.en` model is English anyway)
    force: bool = False  # redo even if a current transcript made with this model exists


class TranscribeStartResponse(BaseModel):
    job_id: str


class TranscriptWordOut(BaseModel):
    start: float  # seconds from the start of the asset's audio
    end: float
    word: str  # no surrounding space; punctuation stays attached
    probability: float


class TranscriptSegmentOut(BaseModel):
    start: float
    end: float
    text: str
    words: list[TranscriptWordOut]


class TranscriptOut(BaseModel):
    """The stored transcript (the document described in `transcribe.py`)."""

    version: int
    asset: str
    model: str
    language: str
    language_probability: float
    duration: float
    vad: bool
    segments: list[TranscriptSegmentOut]


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def asset_of(ctx: RequestContext, asset_id: str) -> assets.Asset:
        try:
            return assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post("/assets/{asset_id}/transcribe", response_model=TranscribeStartResponse)
    def start_transcription(
        asset_id: str, request: TranscribeRequest | None = None, ctx: RequestContext = ctx_dep
    ) -> TranscribeStartResponse:
        """Queue transcribing the asset's audio with word timestamps. The job's result is `{asset, model, language,
        duration, segments, words, made}`; read the words with `GET /assets/{id}/transcript`. 501 (detail: the
        install hint) when the optional faster-whisper package is not installed; 422 for an asset with no audio."""
        request = request or TranscribeRequest()
        asset = asset_of(ctx, asset_id)
        if not asset.has_audio:
            raise HTTPException(status_code=422, detail=f"{asset.path} has no audio track to transcribe")
        try:
            transcribe.require_available()
        except transcribe.TranscriptionUnavailableError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from None
        job_id = jobs.submit(
            TRANSCRIPTION,
            transcribe_work(ctx.media, asset.id, model=request.model, language=request.language, force=request.force),
            label=asset.path,
            total=TRANSCRIBE_STEPS,
            owner=ctx.principal.id,
        )
        return TranscribeStartResponse(job_id=job_id)

    @router.get("/assets/{asset_id}/transcript", response_model=TranscriptOut)
    def get_transcript(
        asset_id: str, start: float | None = None, end: float | None = None, ctx: RequestContext = ctx_dep
    ) -> TranscriptOut:
        """The stored transcript, or with `start` and/or `end` (seconds) only the words overlapping that span. 404
        until a transcription job has run, or after the file changed."""
        asset = asset_of(ctx, asset_id)
        transcript = transcribe.load(ctx.media, asset.id)
        if transcript is None:
            raise HTTPException(status_code=404, detail="no transcript yet: POST /assets/{id}/transcribe to make one")
        return TranscriptOut(**transcribe.excerpt(transcript, start, end))

    return router
