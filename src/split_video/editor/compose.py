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
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.responses import FileResponse

from split_video import timeline, timeline_ops
from split_video.editor.browse import PathEscapesRootError, resolve_within_root
from split_video.ffmpeg import ExtractError, ProbeError, probe_media
from split_video.render import render
from split_video.timeline import Clip, Project, TimelineError


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


def make_router(root: Path) -> APIRouter:
    router = APIRouter()
    root = root.resolve()
    lock = threading.Lock()
    jobs: dict[str, _RenderJob] = {}
    media_cache: dict[tuple[str, int], float] = {}

    def source_duration(source: str) -> float | None:
        try:
            path = timeline.resolve_source(root, source)
            key = (str(path), path.stat().st_mtime_ns)
            if key not in media_cache:
                media_cache[key] = probe_media(path).duration
            return media_cache[key]
        except (TimelineError, OSError, ProbeError):
            return None

    def clip_out(c: Clip) -> ClipOut:
        return ClipOut(
            id=c.id,
            source=c.source,
            in_point=c.in_point,
            out_point=c.out_point,
            start=c.start,
            end=c.end,
            duration=c.duration,
            source_duration=source_duration(c.source),
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

    def path_for(name: str) -> Path:
        try:
            return timeline.project_path(root, name)
        except TimelineError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    def load_existing(name: str) -> tuple[Path, Project]:
        path = path_for(name)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"no project named {name!r}")
        return path, timeline.load(path)

    def source_for(rel: str) -> tuple[str, float]:
        """A root-relative source string and its probed duration, or 4xx."""
        try:
            resolved = resolve_within_root(root, rel)
        except PathEscapesRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root") from None
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        try:
            return timeline.relative_source(root, resolved), probe_media(resolved).duration
        except ProbeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    def default_track(project: Project) -> str:
        track = next((t for t in project.tracks if t.kind == "video"), None)
        if track is None:
            raise HTTPException(status_code=422, detail="the project has no video track")
        return track.id

    def edit(name: str, change):
        """Load `name`, apply `change(project) -> project`, save, return it."""
        with lock:
            path, project = load_existing(name)
            try:
                project = change(project)
            except TimelineError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            timeline.save(path, project)
        return project_out(name, project)

    @router.get("/api/compose/projects", response_model=ProjectListOut)
    def list_projects() -> ProjectListOut:
        return ProjectListOut(projects=timeline.list_projects(root))

    @router.post("/api/compose/projects", response_model=ProjectOut)
    def create_project(request: CreateProjectRequest) -> ProjectOut:
        path = path_for(request.name)
        with lock:
            if path.exists():
                raise HTTPException(status_code=409, detail=f"project {request.name!r} already exists")
            project = timeline.empty_project()
            timeline.save(path, project)
        return project_out(request.name, project)

    @router.get("/api/compose/projects/{name}", response_model=ProjectOut)
    def get_project(name: str) -> ProjectOut:
        _, project = load_existing(name)
        return project_out(name, project)

    @router.post("/api/compose/projects/{name}/clips", response_model=ProjectOut)
    def add_clip(name: str, request: AddClipRequest) -> ProjectOut:
        source, duration = source_for(request.path)

        def change(project: Project) -> Project:
            track_id = request.track_id or default_track(project)
            new, _ = timeline_ops.add_clip(
                project,
                track_id,
                source,
                duration,
                in_point=request.in_point,
                out_point=request.out_point,
                index=request.index,
            )
            return new

        return edit(name, change)

    def duration_of_clip(project: Project, clip_id: str) -> float:
        track, i = timeline_ops.find_clip(project, clip_id)
        duration = source_duration(track.clips[i].source)
        if duration is None:
            raise TimelineError("the clip's source file is missing or unreadable")
        return duration

    @router.patch("/api/compose/projects/{name}/clips/{clip_id}", response_model=ProjectOut)
    def trim_clip(name: str, clip_id: str, request: TrimRequest) -> ProjectOut:
        return edit(
            name,
            lambda p: timeline_ops.trim_clip(
                p, clip_id, duration_of_clip(p, clip_id), in_point=request.in_point, out_point=request.out_point
            ),
        )

    @router.post("/api/compose/projects/{name}/clips/{clip_id}/move", response_model=ProjectOut)
    def move_clip(name: str, clip_id: str, request: MoveRequest) -> ProjectOut:
        return edit(name, lambda p: timeline_ops.move_clip(p, clip_id, request.to_index))

    @router.delete("/api/compose/projects/{name}/clips/{clip_id}", response_model=ProjectOut)
    def remove_clip(name: str, clip_id: str) -> ProjectOut:
        return edit(name, lambda p: timeline_ops.remove_clip(p, clip_id))

    @router.put("/api/compose/projects/{name}/clips/{clip_id}/transition", response_model=ProjectOut)
    def set_transition(name: str, clip_id: str, request: TransitionRequest) -> ProjectOut:
        return edit(name, lambda p: timeline_ops.set_transition(p, clip_id, request.transition, request.duration))

    @router.post("/api/compose/projects/{name}/render", response_model=RenderStartOut)
    def start_render(name: str, request: RenderRequest) -> RenderStartOut:
        _, project = load_existing(name)
        filename = request.output or f"{name}-render.mp4"
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise HTTPException(status_code=422, detail="output must be a plain filename")
        if not filename.lower().endswith(".mp4"):
            raise HTTPException(status_code=422, detail="output must end in .mp4")
        out_path = root / filename
        if out_path.exists() and not request.overwrite:
            raise HTTPException(status_code=409, detail=f"{filename} already exists")
        if not any(t.clips for t in project.tracks):
            raise HTTPException(status_code=422, detail="the timeline has no clips to render")

        job = _RenderJob()
        job_id = uuid.uuid4().hex
        jobs[job_id] = job

        def run() -> None:
            def on_progress(fraction: float) -> None:
                job.progress = fraction

            try:
                render(project, root, out_path, on_progress)
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
