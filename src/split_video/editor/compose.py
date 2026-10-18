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


class TrackOut(BaseModel):
    id: str
    kind: str
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
    index: int | None = None
    in_point: float = 0.0
    out_point: float | None = None


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
        )

    def project_out(name: str, project: Project) -> ProjectOut:
        return ProjectOut(
            name=name,
            duration=project.duration,
            tracks=[TrackOut(id=t.id, kind=t.kind, clips=[clip_out(c) for c in t.clips]) for t in project.tracks],
        )

    def respond(name: str, action: Callable[[], Project]) -> ProjectOut:
        try:
            return project_out(name, action())
        except TimelineError as exc:
            raise _http(exc) from None

    @router.get("/api/compose/projects", response_model=ProjectListOut)
    def list_projects() -> ProjectListOut:
        return ProjectListOut(projects=timeline.list_projects(root))

    @router.post("/api/compose/projects", response_model=ProjectOut)
    def create_project(request: CreateProjectRequest) -> ProjectOut:
        return respond(request.name, lambda: timeline_service.create_project(root, request.name))

    @router.get("/api/compose/projects/{name}", response_model=ProjectOut)
    def get_project(name: str) -> ProjectOut:
        return respond(name, lambda: timeline_service.load_existing(root, name)[1])

    @router.post("/api/compose/projects/{name}/clips", response_model=ProjectOut)
    def add_clip(name: str, request: AddClipRequest) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.add_clip(
                root,
                name,
                request.path,
                track_id=request.track_id,
                index=request.index,
                in_point=request.in_point,
                out_point=request.out_point,
            ),
        )

    @router.patch("/api/compose/projects/{name}/clips/{clip_id}", response_model=ProjectOut)
    def trim_clip(name: str, clip_id: str, request: TrimRequest) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.trim_clip(
                root, name, clip_id, in_point=request.in_point, out_point=request.out_point
            ),
        )

    @router.post("/api/compose/projects/{name}/clips/{clip_id}/move", response_model=ProjectOut)
    def move_clip(name: str, clip_id: str, request: MoveRequest) -> ProjectOut:
        return respond(name, lambda: timeline_service.move_clip(root, name, clip_id, request.to_index))

    @router.delete("/api/compose/projects/{name}/clips/{clip_id}", response_model=ProjectOut)
    def remove_clip(name: str, clip_id: str) -> ProjectOut:
        return respond(name, lambda: timeline_service.remove_clip(root, name, clip_id))

    @router.put("/api/compose/projects/{name}/clips/{clip_id}/transition", response_model=ProjectOut)
    def set_transition(name: str, clip_id: str, request: TransitionRequest) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.set_transition(root, name, clip_id, request.transition, request.duration),
        )

    @router.post("/api/compose/projects/{name}/render", response_model=RenderStartOut)
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

    @router.get("/api/compose/render/{job_id}", response_model=RenderStatusOut)
    def render_status(job_id: str) -> RenderStatusOut:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job id")
        return RenderStatusOut(status=job.status, progress=job.progress, output=job.output, error=job.error)

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
