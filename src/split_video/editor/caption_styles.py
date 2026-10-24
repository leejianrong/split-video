"""`GET /api/v1/caption-styles`: the burned-in caption styles a render can take by name (see `captions.py`)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from split_video import captions


class CaptionStyleOut(BaseModel):
    name: str
    label: str
    description: str
    font_size: float
    primary: str
    highlight: str
    outline: str
    outline_width: float
    shadow: float
    box: bool
    bold: bool
    position: str
    margin_v: float
    max_chars: int
    max_lines: int
    uppercase: bool


class CaptionStylesOut(BaseModel):
    styles: list[CaptionStyleOut]
    positions: list[str]
    font_size_range: list[float]
    default_style: str


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/caption-styles", response_model=CaptionStylesOut)
    def list_caption_styles() -> CaptionStylesOut:
        """Every caption style with all its fields. Pass a `name` as `captions` when starting a render."""
        return CaptionStylesOut(**captions.listing())

    return router
