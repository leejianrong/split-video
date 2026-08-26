"""Pydantic request/response models for the editor's HTTP API."""

from __future__ import annotations

from pydantic import BaseModel


class SilenceIntervalOut(BaseModel):
    start: float
    end: float


class SegmentOut(BaseModel):
    index: int
    start: float
    end: float
    duration: float


class StateParams(BaseModel):
    silence_threshold: float
    min_silence_duration: float
    min_song_length: float
    padding: float


class SessionResponse(BaseModel):
    file_open: bool
    filename: str | None = None


class BrowseEntryOut(BaseModel):
    name: str
    path: str
    is_dir: bool


class BrowseResponse(BaseModel):
    cwd: str
    parent: str | None
    entries: list[BrowseEntryOut]


class OpenRequest(BaseModel):
    path: str


class StateResponse(BaseModel):
    filename: str
    duration: float
    video_url: str
    params: StateParams
    silences: list[SilenceIntervalOut]
    segments: list[SegmentOut]


class DetectRequest(BaseModel):
    silence_threshold: float


class DetectResponse(BaseModel):
    silences: list[SilenceIntervalOut]


class SegmentsRequest(BaseModel):
    silences: list[SilenceIntervalOut]
    duration: float
    min_silence_duration: float
    min_song_length: float
    padding: float


class SegmentsResponse(BaseModel):
    segments: list[SegmentOut]


class ExportSegmentIn(BaseModel):
    start: float
    end: float


class ExportRequest(BaseModel):
    segments: list[ExportSegmentIn]
    precise: bool = False
    output_format: str | None = None
    overwrite: bool = False
    manifest: bool = True


class ExportStartResponse(BaseModel):
    job_id: str


class ExportStatusResponse(BaseModel):
    status: str
    completed: int
    total: int
    current_file: str | None = None
    output_dir: str | None = None
    manifest_path: str | None = None
    files: list[str] | None = None
    error: str | None = None
