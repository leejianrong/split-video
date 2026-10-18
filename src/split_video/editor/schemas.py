"""Pydantic request/response models for the editor's HTTP API."""

from __future__ import annotations

from pydantic import BaseModel

# Project identification convention: every endpoint that works on a video
# takes the project id as a `path` *query parameter* (GET and POST alike;
# request bodies never carry it). The id is the video's path relative to the
# browse root, the same string `/api/open` takes. The server keeps no
# "currently open" file. Escaping the root gives 400, a missing file 404.
PROJECT_PATH_DESCRIPTION = "Project id: the video's path relative to the browse root."


class SilenceIntervalOut(BaseModel):
    start: float
    end: float


class SegmentOut(BaseModel):
    index: int
    start: float
    end: float
    duration: float
    label: str = ""
    color: str | None = None
    included: bool = True
    export_name: str | None = None


class StateParams(BaseModel):
    silence_threshold: float
    min_silence_duration: float
    min_song_length: float
    padding: float


class SessionResponse(BaseModel):
    # The file the server was started on, if any (None when started on a
    # directory). Informational only: no per-server state is kept for it.
    file_open: bool
    filename: str | None = None
    path: str | None = None  # project id to pass as `path` to open that file


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
    resumed: bool = False
    # The project file's revision (0 if never saved). Send it back as `If-Match` on writes.
    revision: int = 0


class DetectRequest(BaseModel):
    silence_threshold: float


class DetectResponse(BaseModel):
    silences: list[SilenceIntervalOut]


class WaveformResponse(BaseModel):
    buckets: list[tuple[float, float]]


class ClassificationRegionOut(BaseModel):
    start: float
    end: float
    bucket: str
    score: float
    secondary: str | None
    scores: dict[str, float]


class ClassificationResponse(BaseModel):
    analyzed: bool
    regions: list[ClassificationRegionOut]
    lanes: dict[str, list[ClassificationRegionOut]]
    thresholds: dict[str, float]


class AnalyzeStartResponse(BaseModel):
    job_id: str


class AnalyzeStatusResponse(BaseModel):
    status: str
    completed: int
    total: int
    error: str | None = None


class RethresholdRequest(BaseModel):
    thresholds: dict[str, float]


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
    name: str | None = None  # custom output basename (no extension); falls back to default numbering if unset


class ProjectSegmentIn(BaseModel):
    start: float
    end: float
    label: str = ""
    color: str | None = None
    included: bool = True
    export_name: str | None = None


class ProjectSaveRequest(BaseModel):
    segments: list[ProjectSegmentIn]


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
