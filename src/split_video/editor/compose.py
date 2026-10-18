"""HTTP API for the timeline ("compose") editor.

A separate router from the split editor's endpoints in `app.py`: it works on
timeline projects (`timeline.py`) stored in the browse root, not on the one
open video. Each request loads the project file, applies one operation from
`timeline_ops`, and saves it back, so the file on disk is always the truth
and the CLI/MCP can edit the same project.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.responses import FileResponse

from split_video import timeline, timeline_service
from split_video.editor.browse import PathEscapesRootError, resolve_within_root
from split_video.ffmpeg import ExtractError, ProbeError
from split_video.timeline import (
    Clip,
    OutputExistsError,
    OutsideRootError,
    Project,
    ProjectExistsError,
    ProjectNotFoundError,
    SourceNotFoundError,
    TimelineError,
)


class ClipOut(BaseModel):
    id: str
    source: str
    in_point: float
    out_point: float
    start: float
    end: float
    duration: float
    source_duration: float | None  # None if the source file is missing
    gain: float
    muted: bool
    transition: str
    transition_duration: float
    x: float
    y: float
    scale: float
    opacity: float
    brightness: float
    contrast: float
    saturation: float
    kind: str
    text: str
    font_size: float
    color: str


class TrackOut(BaseModel):
    id: str
    kind: str
    magnetic: bool
    clips: list[ClipOut]


class ProjectOut(BaseModel):
    name: str
    duration: float
    tracks: list[TrackOut]


class ProjectListOut(BaseModel):
    projects: list[str]


class CreateProjectRequest(BaseModel):
    name: str


class AddClipRequest(BaseModel):
    track_id: str | None = None  # defaults to the first video track
    path: str  # relative to the browse root
    index: int | None = None  # position on the main track
    start: float | None = None  # seconds, on any other track
    in_point: float = 0.0
    out_point: float | None = None


class AddTrackRequest(BaseModel):
    kind: str


class MoveTrackRequest(BaseModel):
    to_index: int


class PlaceRequest(BaseModel):
    start: float | None = None
    track_id: str | None = None


class PropertiesRequest(BaseModel):
    """Any subset of a clip's gain/mute, overlay placement and colour adjustment."""

    gain: float | None = None
    muted: bool | None = None
    x: float | None = None
    y: float | None = None
    scale: float | None = None
    opacity: float | None = None
    brightness: float | None = None
    contrast: float | None = None
    saturation: float | None = None


class AddTextRequest(BaseModel):
    text: str
    duration: float = 3.0
    start: float = 0.0
    font_size: float = 0.08
    color: str = "#ffffff"
    x: float = 0.5
    y: float = 0.85


class TextRequest(BaseModel):
    text: str | None = None
    font_size: float | None = None
    color: str | None = None


class TrimRequest(BaseModel):
    in_point: float | None = None
    out_point: float | None = None


class MoveRequest(BaseModel):
    to_index: int


class TransitionRequest(BaseModel):
    transition: str
    duration: float = 0.0


class RenderRequest(BaseModel):
    output: str | None = None  # filename in the browse root; default "<name>-render.mp4"
    overwrite: bool = False


class RenderStartOut(BaseModel):
    job_id: str


class RenderStatusOut(BaseModel):
    status: str
    progress: float
    output: str | None = None
    error: str | None = None


@dataclass
class _RenderJob:
    status: str = "running"
    progress: float = 0.0
    output: str | None = None
    error: str | None = None


def _http(exc: TimelineError) -> HTTPException:
    """The HTTP status that best matches a timeline failure."""
    if isinstance(exc, ProjectNotFoundError | SourceNotFoundError):
        status = 404
    elif isinstance(exc, ProjectExistsError | OutputExistsError):
        status = 409
    elif isinstance(exc, OutsideRootError):
        status = 400
    else:
        status = 422
    return HTTPException(status_code=status, detail=str(exc))


def make_router(root: Path) -> APIRouter:
    router = APIRouter()
    root = root.resolve()
    jobs: dict[str, _RenderJob] = {}

    def clip_out(c: Clip) -> ClipOut:
        return ClipOut(
            id=c.id,
            source=c.source,
            in_point=c.in_point,
            out_point=c.out_point,
            start=c.start,
            end=c.end,
            duration=c.duration,
            source_duration=timeline_service.source_duration(root, c.source),
            gain=c.gain,
            muted=c.muted,
            transition=c.transition,
            transition_duration=c.transition_duration,
            x=c.x,
            y=c.y,
            scale=c.scale,
            opacity=c.opacity,
            brightness=c.brightness,
            contrast=c.contrast,
            saturation=c.saturation,
            kind=c.kind,
            text=c.text,
            font_size=c.font_size,
            color=c.color,
        )

    def project_out(name: str, project: Project) -> ProjectOut:
        return ProjectOut(
            name=name,
            duration=project.duration,
            tracks=[
                TrackOut(id=t.id, kind=t.kind, magnetic=t.magnetic, clips=[clip_out(c) for c in t.clips])
                for t in project.tracks
            ],
        )

    def respond(name: str, action: Callable[[], Project]) -> ProjectOut:
        try:
            return project_out(name, action())
        except TimelineError as exc:
            raise _http(exc) from None

    @router.get("/compose/projects", response_model=ProjectListOut)
    def list_projects() -> ProjectListOut:
        return ProjectListOut(projects=timeline.list_projects(root))

    @router.post("/compose/projects", response_model=ProjectOut)
    def create_project(request: CreateProjectRequest) -> ProjectOut:
        return respond(request.name, lambda: timeline_service.create_project(root, request.name))

    @router.get("/compose/projects/{name}", response_model=ProjectOut)
    def get_project(name: str) -> ProjectOut:
        return respond(name, lambda: timeline_service.load_existing(root, name)[1])

    @router.post("/compose/projects/{name}/clips", response_model=ProjectOut)
    def add_clip(name: str, request: AddClipRequest) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.add_clip(
                root,
                name,
                request.path,
                track_id=request.track_id,
                index=request.index,
                start=request.start,
                in_point=request.in_point,
                out_point=request.out_point,
            ),
        )

    @router.patch("/compose/projects/{name}/clips/{clip_id}", response_model=ProjectOut)
    def trim_clip(name: str, clip_id: str, request: TrimRequest) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.trim_clip(
                root, name, clip_id, in_point=request.in_point, out_point=request.out_point
            ),
        )

    @router.post("/compose/projects/{name}/tracks", response_model=ProjectOut)
    def add_track(name: str, request: AddTrackRequest) -> ProjectOut:
        return respond(name, lambda: timeline_service.add_track(root, name, request.kind))

    @router.delete("/compose/projects/{name}/tracks/{track_id}", response_model=ProjectOut)
    def remove_track(name: str, track_id: str) -> ProjectOut:
        return respond(name, lambda: timeline_service.remove_track(root, name, track_id))

    @router.post("/compose/projects/{name}/tracks/{track_id}/move", response_model=ProjectOut)
    def move_track(name: str, track_id: str, request: MoveTrackRequest) -> ProjectOut:
        return respond(name, lambda: timeline_service.move_track(root, name, track_id, request.to_index))

    @router.patch("/compose/projects/{name}/clips/{clip_id}/properties", response_model=ProjectOut)
    def set_properties(name: str, clip_id: str, request: PropertiesRequest) -> ProjectOut:
        changes = request.model_dump(exclude_none=True)
        return respond(name, lambda: timeline_service.set_clip_properties(root, name, clip_id, **changes))

    @router.post("/compose/projects/{name}/tracks/{track_id}/text", response_model=ProjectOut)
    def add_text(name: str, track_id: str, request: AddTextRequest) -> ProjectOut:
        options = request.model_dump(exclude={"text", "duration"})
        return respond(
            name,
            lambda: timeline_service.add_text_clip(root, name, track_id, request.text, request.duration, **options),
        )

    @router.patch("/compose/projects/{name}/clips/{clip_id}/text", response_model=ProjectOut)
    def set_text(name: str, clip_id: str, request: TextRequest) -> ProjectOut:
        changes = request.model_dump(exclude_none=True)
        return respond(name, lambda: timeline_service.set_text_properties(root, name, clip_id, **changes))

    @router.post("/compose/projects/{name}/clips/{clip_id}/place", response_model=ProjectOut)
    def place_clip(name: str, clip_id: str, request: PlaceRequest) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.place_clip(root, name, clip_id, start=request.start, track_id=request.track_id),
        )

    @router.post("/compose/projects/{name}/clips/{clip_id}/move", response_model=ProjectOut)
    def move_clip(name: str, clip_id: str, request: MoveRequest) -> ProjectOut:
        return respond(name, lambda: timeline_service.move_clip(root, name, clip_id, request.to_index))

    @router.delete("/compose/projects/{name}/clips/{clip_id}", response_model=ProjectOut)
    def remove_clip(name: str, clip_id: str) -> ProjectOut:
        return respond(name, lambda: timeline_service.remove_clip(root, name, clip_id))

    @router.put("/compose/projects/{name}/clips/{clip_id}/transition", response_model=ProjectOut)
    def set_transition(name: str, clip_id: str, request: TransitionRequest) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.set_transition(root, name, clip_id, request.transition, request.duration),
        )

    @router.post("/compose/projects/{name}/render", response_model=RenderStartOut)
    def start_render(name: str, request: RenderRequest) -> RenderStartOut:
        try:
            project, filename, out_path = timeline_service.plan_render(root, name, request.output, request.overwrite)
        except TimelineError as exc:
            raise _http(exc) from None

        job = _RenderJob()
        job_id = uuid.uuid4().hex
        jobs[job_id] = job

        def run() -> None:
            def on_progress(fraction: float) -> None:
                job.progress = fraction

            try:
                timeline_service.run_render(project, root, out_path, on_progress)
                job.output = filename
                job.status = "done"
            except (TimelineError, ExtractError, ProbeError, OSError) as exc:
                job.error = str(exc)
                job.status = "error"

        threading.Thread(target=run, daemon=True).start()
        return RenderStartOut(job_id=job_id)

    @router.get("/compose/render/{job_id}", response_model=RenderStatusOut)
    def render_status(job_id: str) -> RenderStatusOut:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job id")
        return RenderStatusOut(status=job.status, progress=job.progress, output=job.output, error=job.error)

    return router


def make_media_router(root: Path) -> APIRouter:
    router = APIRouter()
    root = root.resolve()

    @router.get("/media/file")
    def media_file(path: str) -> FileResponse:
        """A video under the browse root, for the compose page's preview player."""
        try:
            resolved = resolve_within_root(root, path)
        except PathEscapesRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root") from None
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(resolved)

    return router
