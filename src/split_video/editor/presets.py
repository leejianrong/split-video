"""`GET /api/v1/presets`: the export presets a render can take by name (see `presets.py`)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from split_video import presets


class PresetOut(BaseModel):
    name: str
    label: str
    description: str
    width: int
    height: int
    fps: int
    video_codec: str
    x264_preset: str
    crf: int
    pix_fmt: str
    audio_codec: str
    audio_bitrate: str
    audio_sample_rate: int
    container: str
    faststart: bool


class PresetsOut(BaseModel):
    presets: list[PresetOut]
    default_fit: str
    fits: list[str]


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/presets", response_model=PresetsOut)
    def list_presets() -> PresetsOut:
        """Every export preset with all its fields. Pass a `name` as `preset` when starting a render."""
        return PresetsOut(**presets.listing())

    return router
