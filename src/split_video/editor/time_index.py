"""HTTP routes for what is happening at a time of an asset (see `time_index.py`).

`GET /assets/{id}/at?time=&window=` returns every stored signal around `time` (audio class, loudness, silences,
words, scene cuts and activity, on-screen text) in one JSON document; `GET /assets/{id}/timeline?start=&end=&step=`
returns compact points over a span. Missing signals are `null` with a `missing` hint, never an error. 404 for an
unknown asset, 422 for a bad time, window, span or step. The CLI (`at`, `timeline-index`) and MCP
(`what_is_happening`, `timeline_index`) return the same JSON.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from split_video import assets, time_index
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.media_store import MediaError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError


class SpanOut(BaseModel):
    start: float
    end: float


class LoudnessOut(BaseModel):
    mean_db: float | None
    max_db: float | None
    min_db: float | None
    window_seconds: float
    curve: list[dict[str, float]]  # {t, db}


class SilenceOut(BaseModel):
    threshold_db: float
    inside: bool  # `time` is in a silence and no transcribed word is being spoken then
    inside_exact: bool  # `time` is in a listed silence interval, whatever the transcript says
    intervals: list[SpanOut]
    truncated: bool


class WordOut(BaseModel):
    start: float
    end: float
    word: str


class SpeechOut(BaseModel):
    words: list[WordOut]
    text: str
    word_count: int
    words_per_minute: float
    truncated: bool


class SceneChangeOut(BaseModel):
    time: float
    score: float
    kind: str  # "cut" (scdet) or "screen" (a text-only change on the same background)


class ActivityOut(BaseModel):
    mean: float
    max: float
    curve: list[dict[str, float]]  # {t, value}


class OnScreenTextOut(BaseModel):
    time: float
    until: float
    age_seconds: float  # `time` minus the last OCR frame that saw this text
    stale: bool  # older than 2 OCR intervals, or the picture changed after it was read
    note: str | None  # why it is stale
    text: str
    lines: list[str]
    truncated: bool
    more: int


class VisualOut(BaseModel):
    scene_change: SceneChangeOut | None
    scene_changes: int | None
    scene_index: int | None
    shot_start: float | None
    activity: ActivityOut | None
    description: str | None  # reserved: always null for now
    text: OnScreenTextOut | None


class MissingOut(BaseModel):
    signal: str
    how: str


class AtOut(BaseModel):
    """What is happening around a time (`time_index.what_is_happening`, version 1)."""

    version: int
    asset: str
    time: float
    window: SpanOut
    duration: float
    audio: dict[str, Any] | None  # {class, share, secondary, regions, regions_truncated}; `class` is a keyword
    loudness: LoudnessOut | None
    silence: SilenceOut | None
    speech: SpeechOut | None
    visual: VisualOut
    available: dict[str, bool]
    missing: list[MissingOut]


class PointOut(BaseModel):
    start: float
    end: float
    audio: str | None
    loudness_db: float | None
    loudness_max_db: float | None
    word_count: int | None
    words_per_minute: float | None
    scene_changes: int | None
    activity: float | None
    text: str | None


class TimelineOut(BaseModel):
    """Compact points over a span (`time_index.timeline_index`, version 1)."""

    version: int
    asset: str
    duration: float
    start: float
    end: float
    step: float
    count: int
    points: list[PointOut]
    available: dict[str, bool]
    missing: list[MissingOut]


def make_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def read(call, asset_id: str, ctx: RequestContext, *args: Any) -> dict[str, Any]:
        try:
            asset = assets.get_asset(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise api_error(404, exc) from None
        try:
            return call(ctx.media, asset.id, *args)
        except time_index.TimeIndexError as exc:
            raise api_error(422, exc) from None

    @router.get("/assets/{asset_id}/at", response_model=AtOut)
    def what_is_happening(
        asset_id: str, time: float, window: float | None = None, ctx: RequestContext = ctx_dep
    ) -> dict[str, Any]:
        """Everything stored about the asset around `time` (seconds): the audio class, loudness, silences, words
        spoken, scene cuts and screen activity, and on-screen text, in a `window` (seconds, default 10, at most 600)
        centred on `time` and clamped to the asset. A signal that was never computed is `null`; `available` and
        `missing` (with how to make each) say which. Never decodes or runs a model. 404 for an unknown asset; 422 for
        a negative time, a time beyond the duration or a bad window."""
        return read(time_index.what_is_happening, asset_id, ctx, time, window)

    @router.get("/assets/{asset_id}/timeline", response_model=TimelineOut)
    def timeline_index(
        asset_id: str,
        start: float | None = None,
        end: float | None = None,
        step: float | None = None,
        ctx: RequestContext = ctx_dep,
    ) -> dict[str, Any]:
        """Compact points, one per `step` seconds (default 60, raised so that there are at most 500 points) from
        `start` to `end` (default the whole asset): audio class, mean and max loudness, word count and rate, scene
        cuts, mean activity and the first line of on-screen text. 422 when the span, step or number of points is
        not allowed."""
        return read(time_index.timeline_index, asset_id, ctx, start, end, step)

    return router
