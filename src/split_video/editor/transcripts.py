"""HTTP routes for an asset's transcript (see `transcribe.py`).

`POST /assets/{id}/transcribe` queues a `transcription` job (poll `/jobs/{job_id}`; progress is thousandths of
the audio) and `GET /assets/{id}/transcript` returns the stored transcript (404 if there is none yet). With `limit`, `cursor`,
`granularity` or `max_chars` it returns one page instead (`transcript_search.transcript_range`), and
`GET /assets/{id}/transcript/search?q=` finds a phrase (`transcript_search.transcript_search`): 404 for no
transcript, 422 for a bad range, limit, cursor or query.

Without the optional faster-whisper package the POST answers 501 with `transcribe.UNAVAILABLE` (the install
hint) as its `detail`, before queuing anything; an asset with no audio is a 422. The CLI (`transcribe`, exit
code 4) and MCP (`transcribe_asset`, a tool error) report the same text.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from split_video import assets, transcribe, transcript_search
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
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


class RangeBoundsOut(BaseModel):
    start: float
    end: float | None  # null: to the end


class CoveredOut(BaseModel):
    start: float
    end: float


class RangeSegmentOut(BaseModel):
    index: int  # position in the whole transcript, from 0
    start: float
    end: float
    text: str


class RangeWordOut(BaseModel):
    index: int
    segment: int  # index of the segment holding the word
    start: float
    end: float
    word: str
    probability: float


class TranscriptPageOut(BaseModel):
    """One page of the transcript (`transcript_search.transcript_range`)."""

    asset: str
    granularity: str  # segment | word
    duration: float
    range: RangeBoundsOut
    total_in_range: int
    count: int
    covered: CoveredOut | None
    items: list[RangeSegmentOut | RangeWordOut]
    next_cursor: str | None  # pass back as `cursor` for the next page; null on the last


class SearchHitOut(BaseModel):
    time: float  # start of the first matched word
    end: float
    text: str
    context_before: str
    context_after: str
    segment: int
    word: int


class TranscriptSearchOut(BaseModel):
    """One page of hits for a phrase (`transcript_search.transcript_search`)."""

    asset: str
    query: str
    range: RangeBoundsOut
    total_matches: int
    count: int
    hits: list[SearchHitOut]
    next_cursor: str | None


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def asset_of(ctx: RequestContext, asset_id: str) -> assets.Asset:
        try:
            return assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise api_error(404, exc) from None

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
            raise api_error(501, exc) from None
        job_id = jobs.submit(
            TRANSCRIPTION,
            transcribe_work(ctx.media, asset.id, model=request.model, language=request.language, force=request.force),
            label=asset.path,
            total=TRANSCRIBE_STEPS,
            owner=ctx.principal.id,
        )
        return TranscribeStartResponse(job_id=job_id)

    def read(call, asset_id: str, ctx: RequestContext, *args, **kwargs):
        asset = asset_of(ctx, asset_id)
        try:
            return call(ctx.media, asset.id, *args, **kwargs)
        except transcript_search.TranscriptNotFoundError as exc:
            raise api_error(404, exc) from None
        except transcript_search.TranscriptQueryError as exc:
            raise api_error(422, exc) from None

    @router.get("/assets/{asset_id}/transcript", response_model=TranscriptOut | TranscriptPageOut)
    def get_transcript(
        asset_id: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        granularity: str | None = None,
        max_chars: int | None = None,
        ctx: RequestContext = ctx_dep,
    ) -> TranscriptOut | TranscriptPageOut:
        """The stored transcript, or with `start` and/or `end` (seconds) only the words overlapping that span. With
        any of `limit`, `cursor`, `granularity` (`segment` or `word`) or `max_chars` it is one page of the segments
        (or words) overlapping the span instead, with `total_in_range` and a `next_cursor` to pass back until it is
        null; a page never holds more than 500 items. 404 until a transcription job has run, or after the file
        changed; 422 for a bad span, limit or cursor."""
        if limit is None and cursor is None and granularity is None and max_chars is None:
            asset = asset_of(ctx, asset_id)
            transcript = transcribe.load(ctx.media, asset.id)
            if transcript is None:
                raise HTTPException(status_code=404, detail=f"no transcript yet: {transcript_search.NOT_FOUND_HINT}")
            return TranscriptOut(**transcribe.excerpt(transcript, start, end))
        page = read(
            transcript_search.transcript_range,
            asset_id,
            ctx,
            start,
            end,
            limit=limit,
            cursor=cursor,
            granularity=granularity or "segment",
            max_chars=max_chars,
        )
        return TranscriptPageOut(**page)

    @router.get("/assets/{asset_id}/transcript/search", response_model=TranscriptSearchOut)
    def search_transcript(
        asset_id: str,
        q: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        context: int | None = None,
        case_sensitive: bool = False,
        ctx: RequestContext = ctx_dep,
    ) -> TranscriptSearchOut:
        """Where the phrase `q` is said: case-insensitive whole-word match, punctuation ignored, across segment
        boundaries. Hits in time order with `time`, `end`, `text`, `context_before` / `context_after` (`context`
        words each, default 8, at most 50), `segment` and `word` indices; `limit` per page (default 20, at most
        200) and a `next_cursor`. `start` / `end` keep hits whose first word overlaps that span. 404 without a
        transcript; 422 for an empty or over-long query (500 characters), a bad span, limit or cursor."""
        found = read(
            transcript_search.transcript_search,
            asset_id,
            ctx,
            q,
            start=start,
            end=end,
            limit=limit,
            cursor=cursor,
            context=context,
            case_sensitive=case_sensitive,
        )
        return TranscriptSearchOut(**found)

    return router
