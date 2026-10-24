"""HTTP routes to find what was said or shown (see `search.py`).

`GET /assets/{id}/search?q=` searches the transcript and the on-screen text together and returns ranked moments (a
thing said and shown at about the same time is one hit with both as `evidence`); `sources=transcript,ocr` limits it.
`GET /assets/{id}/search/visual?q=` searches what the screen showed only (the on-screen text search of
`GET /assets/{id}/ocr/search`, plus a `sources` summary). 404 when none of the requested sources has data (the
detail names how to make it), 422 for a bad query, span, limit, cursor or source name. The CLI (`search`,
`visual-search`) and MCP (`search_asset`, `visual_search`) return the same JSON.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from split_video import assets, ocr, search, transcript_search
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.editor.ocr import OcrSearchOut
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError


class SearchRangeOut(BaseModel):
    start: float
    end: float | None  # null: to the end


class EvidenceOut(BaseModel):
    """One piece of what a moment is made of: a hit of one source (the source's own fields come along: `segment` and
    `word` for the transcript; `frame` and `matched_tokens` for on-screen text)."""

    model_config = ConfigDict(extra="allow")

    source: str  # "transcript" or "ocr"
    time: float
    end: float
    score: float  # 0..1, within its source
    text: str
    context_before: str
    context_after: str
    match: str  # "phrase" (the whole query) or "words" (one word of it)
    covers: list[str]  # the query words it matched


class MomentOut(BaseModel):
    source: str  # the source of the best evidence; text and context come from it
    time: float  # the earliest evidence time: where to seek
    end: float
    score: float  # 0..1, comparable across sources (see search.py)
    text: str
    context_before: str
    context_after: str
    match: str
    found_in: list[str]  # sources with evidence, best first
    evidence: list[EvidenceOut]


class SearchOut(BaseModel):
    """One page of moments (`search.search_asset`)."""

    asset: str
    query: str
    tokens: list[str]  # the query's content words
    range: SearchRangeOut
    sources: dict[str, str]  # per source: "searched", "not run: <how to make it>" or "not requested"
    total_matches: int
    count: int
    hits: list[MomentOut]
    next_cursor: str | None


class VisualSearchOut(OcrSearchOut):
    """One page of on-screen text hits (`search.visual_search`)."""

    sources: dict[str, str]


def make_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def read(call, asset_id: str, ctx: RequestContext, *args: Any, **kwargs: Any):
        try:
            asset = assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise api_error(404, exc) from None
        try:
            return call(ctx.media, asset.id, *args, **kwargs)
        except (search.SearchNotFoundError, ocr.OcrNotFoundError, transcript_search.TranscriptNotFoundError) as exc:
            raise api_error(404, exc) from None
        except (search.SearchQueryError, ocr.OcrArgumentError, transcript_search.TranscriptQueryError) as exc:
            raise api_error(422, exc) from None

    @router.get("/assets/{asset_id}/search", response_model=SearchOut)
    def search_asset(
        asset_id: str,
        q: str,
        sources: str | None = None,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        context: int | None = None,
        ctx: RequestContext = ctx_dep,
    ) -> SearchOut:
        """Find where `q` was said or shown, in the transcript and the on-screen text together. Hits are moments,
        best first: `source`, `time` (seconds, where to seek), `end`, `score` (0..1, comparable across sources:
        phrase in one source about 0.9 to 1.0, said AND shown higher, one word of a longer query about half),
        `text`, `context_before` / `context_after` (`context` words or lines, at most 10), `found_in`, `match` and
        `evidence` (each source's own hit). Things said and shown within 15 s are one moment. `sources` (comma
        separated: transcript, ocr; default both) limits the search; the `sources` in the answer says what was
        searched and how to make what is missing. `start` / `end` keep hits in that span; `limit` per page (default
        20, at most 200) and a `next_cursor` to pass back until it is null. 404 when none of the requested sources
        has data; 422 for a bad query (500 characters at most), source, span, limit or cursor."""
        return SearchOut(
            **read(
                search.search_asset,
                asset_id,
                ctx,
                q,
                sources=sources,
                start=start,
                end=end,
                limit=limit,
                cursor=cursor,
                context=context,
            )
        )

    @router.get("/assets/{asset_id}/search/visual", response_model=VisualSearchOut)
    def search_visual(
        asset_id: str,
        q: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        context: int | None = None,
        ctx: RequestContext = ctx_dep,
    ) -> VisualSearchOut:
        """Find where `q` was on the screen: the on-screen text search (as `GET /assets/{id}/ocr/search`: fuzzy,
        token-based, best first, `source: "ocr"`, `score` 0..1, `time`, `end`, `text`, context lines) plus `sources`,
        which visual sources were searched. 404 without stored on-screen text (run an ocr job); 422 for a bad
        query, span, limit or cursor."""
        return VisualSearchOut(
            **read(
                search.visual_search,
                asset_id,
                ctx,
                q,
                start=start,
                end=end,
                limit=limit,
                cursor=cursor,
                context=context,
            )
        )

    return router
