"""HTTP route for highlight suggestions of an asset (see `highlights.py`).

`GET /assets/{id}/highlights?preset=&count=&min_seconds=&max_seconds=&target_seconds=&start=&end=&keywords=&weights=
&min_gap_seconds=&snap=` returns ranked, non-overlapping candidate windows scored from the stored signals, each with the
reasons it was chosen. It reads documents only: nothing is decoded and no model runs. `keywords` is comma separated;
`weights` is a JSON object (`{"speech": 2}`) or `signal:weight,...` (`speech:2,applause:0.5`). 404 for an unknown asset,
422 for bad parameters (unknown preset or weight signal, a negative weight, a bad span), 409 when no signal is stored
yet (the message names what to run). The CLI (`highlights --json`) and MCP (`suggest_highlights`) return the same JSON.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from split_video import assets, edit_ops, highlight_review, highlights
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.editor.ops import OPS_RESPONSES, OpsResultOut, invalid_response
from split_video.editor.revisions import WRITE_RESPONSES, base_revision
from split_video.errors import ProjectError, RevisionConflictError
from split_video.ffmpeg import ProbeError
from split_video.media_store import MediaError, MediaOutsideRootError
from split_video.store_provider import StoreProvider
from split_video.timeline import AssetNotFoundError, TimelineError


class HighlightsOut(BaseModel):
    """`highlights.suggest_highlights` (version 1). The nested parts are documented in docs/highlights.md; they are
    plain JSON so that the shape is identical on every surface."""

    version: int
    asset: str
    preset: str
    params: dict[str, Any]
    signals: dict[str, Any]  # {available, missing, used}
    candidates: list[dict[str, Any]]  # {rank, start, end, duration, score, reasons, score_parts, snapped, ...}
    notes: list[str]


class ReviewEntryOut(BaseModel):
    state: str  # accepted | dismissed | pending (pending: only the adjusted edges are kept)
    start: float  # the window the engine scored
    end: float
    adjusted: dict[str, float] | None = None  # {start, end}: the user's own edges, when they changed them


class ReviewsOut(BaseModel):
    """`highlight_review.load`: the reviews of an asset's suggestions, by suggestion `key` (`h_<start ms>_<end ms>`)."""

    version: int
    revision: int  # writes send it as If-Match
    reviews: dict[str, ReviewEntryOut]


class ReviewIn(BaseModel):
    state: Literal["accepted", "dismissed", "pending"] | None  # null clears the review (pending again)
    start: float  # the scored window of the suggestion (its key is made from it)
    end: float
    adjusted_start: float | None = None  # the user's own edges, both or neither
    adjusted_end: float | None = None


class MakeItemIn(BaseModel):
    start: float  # the edges to cut (adjusted ones if the user trimmed the suggestion)
    end: float
    title: str = ""
    scored: dict[str, float] | None = None  # {start, end}: the window the engine scored; default start / end


class MakeClipsIn(BaseModel):
    asset: str  # the asset id the highlights are of
    kind: Literal["short", "clip"] = "short"  # short: cropped to 9:16; clip: the whole frame
    items: list[MakeItemIn] = Field(min_length=1, max_length=highlight_review.MAX_ITEMS)


class MadeOut(BaseModel):
    key: str
    clip: str
    label: str
    start: float
    end: float


class MakeClipsOut(OpsResultOut):
    reviews: ReviewsOut  # the review document after: the made highlights are accepted
    made: list[MadeOut]


def make_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    @router.get("/assets/{asset_id}/highlights", response_model=HighlightsOut)
    def get_highlights(
        asset_id: str,
        preset: str = "default",
        count: int = highlights.DEFAULT_COUNT,
        min_seconds: float = highlights.DEFAULT_MIN_SECONDS,
        max_seconds: float = highlights.DEFAULT_MAX_SECONDS,
        target_seconds: float | None = None,
        start: float | None = None,
        end: float | None = None,
        keywords: str | None = None,
        weights: str | None = None,
        min_gap_seconds: float = highlights.DEFAULT_MIN_GAP,
        snap: bool = True,
        ctx: RequestContext = ctx_dep,
    ) -> dict[str, Any]:
        """Suggest highlight candidates: windows scored from the stored speech density, loudness, applause / music
        share, scene changes, screen activity, on-screen text and `keywords` (comma separated), minus silence, picked
        without overlap and snapped to silences, sentences and scene changes, ranked with the reasons for each.
        `preset` is default, gig, stream or lecture; `weights` overrides single weights (`speech:2,applause:0.5` or a
        JSON object). Start and end of each candidate are seconds on the asset's timeline. Reads stored signals only.
        404 for an unknown asset; 422 for bad parameters; 409 when no signal is stored."""
        try:
            asset = assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise api_error(404, exc) from None
        try:
            return highlights.suggest_highlights(
                ctx.media,
                asset.id,
                preset=preset,
                count=count,
                min_seconds=min_seconds,
                max_seconds=max_seconds,
                target_seconds=target_seconds,
                weights=weights,
                keywords=keywords,
                start=start,
                end=end,
                min_gap_seconds=min_gap_seconds,
                snap=snap,
            )
        except highlights.NoSignalsError as exc:
            raise api_error(409, exc) from None
        except highlights.HighlightsError as exc:
            raise api_error(422, exc) from None

    @router.get("/assets/{asset_id}/highlight-reviews", response_model=ReviewsOut)
    def get_highlight_reviews(asset_id: str, ctx: RequestContext = ctx_dep) -> dict[str, Any]:
        """Which of the asset's highlight suggestions were accepted or dismissed, and any edges the user adjusted
        (a sidecar next to the video). A suggestion with no entry is not reviewed yet. 404 for an unknown asset."""
        try:
            return highlight_review.load(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise api_error(404, exc) from None

    @router.put("/assets/{asset_id}/highlight-reviews/{key}", response_model=ReviewsOut, responses=WRITE_RESPONSES)
    def put_highlight_review(
        asset_id: str,
        key: str,
        body: ReviewIn,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> dict[str, Any]:
        """Accept or dismiss one suggestion (`key` is `h_<start ms>_<end ms>` of its scored window `start`..`end`), or
        clear its review with `state: null` (undo of a dismissal). `adjusted_start` and `adjusted_end` keep the
        user's own edges. `If-Match` is the `revision` of the reviews document. 404 unknown asset, 422 bad range or
        state, 409 stale revision."""
        try:
            return highlight_review.set_review(
                ctx.media,
                asset_id,
                key,
                body.state,
                start=body.start,
                end=body.end,
                adjusted_start=body.adjusted_start,
                adjusted_end=body.adjusted_end,
                base_revision=base,
            )
        except RevisionConflictError:
            raise
        except AssetNotFoundError as exc:
            raise api_error(404, exc) from None
        except (highlights.HighlightsError, TimelineError, MediaError) as exc:
            raise api_error(422, exc) from None

    @router.post(
        "/project/highlights",
        response_model=MakeClipsOut,
        response_model_exclude_none=True,
        responses=OPS_RESPONSES,
    )
    def make_highlight_clips(
        body: Annotated[MakeClipsIn, Body()],
        path: str = Query(..., description="The video's path relative to the browse root (the project id)."),
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> Any:
        """Make highlights into labelled clips at the end of the video's main track, in one atomic edit-ops batch
        (`kind` `short`: cropped to the centre 9:16, `clip`: the whole frame), and mark them accepted. `If-Match` is
        the project's revision (`GET /project/timeline`). The answer is the ops result plus `made` and the new
        `reviews`. 404 unknown asset, 422 a bad range or a refused batch (`ops_invalid` report), 409 stale revision."""
        try:
            source = ctx.media.resolve(path)
        except MediaOutsideRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root") from None
        if not ctx.media.is_file(source):
            raise HTTPException(status_code=404, detail="file not found")
        try:
            return highlight_review.make_clips(
                ctx.media,
                ctx.store,
                source,
                body.asset,
                [i.model_dump() for i in body.items],
                body.kind,
                base_revision=base,
            )
        except edit_ops.OpsInvalidError as exc:
            return invalid_response(exc)
        except RevisionConflictError:
            raise
        except AssetNotFoundError as exc:
            raise api_error(404, exc) from None
        except (highlights.HighlightsError, TimelineError, MediaError, ProbeError, ProjectError) as exc:
            raise api_error(422, exc) from None

    return router
