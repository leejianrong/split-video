"""HTTP route for highlight suggestions of an asset (see `highlights.py`).

`GET /assets/{id}/highlights?preset=&count=&min_seconds=&max_seconds=&target_seconds=&start=&end=&keywords=&weights=
&min_gap_seconds=&snap=` returns ranked, non-overlapping candidate windows scored from the stored signals, each with the
reasons it was chosen. It reads documents only: nothing is decoded and no model runs. `keywords` is comma separated;
`weights` is a JSON object (`{"speech": 2}`) or `signal:weight,...` (`speech:2,applause:0.5`). 404 for an unknown asset,
422 for bad parameters (unknown preset or weight signal, a negative weight, a bad span), 409 when no signal is stored
yet (the message names what to run). The CLI (`highlights --json`) and MCP (`suggest_highlights`) return the same JSON.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from split_video import assets, highlights
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError


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

    return router
