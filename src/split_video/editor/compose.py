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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import Response

from split_video import assets, timeline_service
from split_video.editor.media_response import media_response
from split_video.editor.revisions import WRITE_RESPONSES, base_revision
from split_video.errors import RevisionConflictError
from split_video.ffmpeg import ExtractError, ProbeError
from split_video.media_store import MediaError, MediaOutsideRootError, MediaStore
from split_video.store import ProjectStore
from split_video.timeline import (
    AssetNotFoundError,
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
    asset: str  # the clip's asset id (see /assets); "" for a text clip or a media clip not yet linked
    source: str  # where the asset's file is now (last known path if it can't be found)
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
    # The project file's revision; send it back as the `If-Match` header on writes (see editor/revisions.py).
    revision: int = 0
    duration: float
    tracks: list[TrackOut]


class ProjectListOut(BaseModel):
    projects: list[str]


class CreateProjectRequest(BaseModel):
    name: str


class AssetOut(BaseModel):
    id: str
    path: str  # the file's current location, relative to the browse root
    size: int
    mtime: float
    duration: float
    has_audio: bool
    width: int | None
    height: int | None
    fps: float | None
    copies: list[str]  # other paths known to hold the same bytes
    present: bool  # whether the file is at `path` with this content right now


class AssetListOut(BaseModel):
    assets: list[AssetOut]


class RegisterAssetRequest(BaseModel):
    path: str  # relative to the browse root


class AddClipRequest(BaseModel):
    track_id: str | None = None  # defaults to the first video track
    path: str | None = None  # a file relative to the browse root; give exactly one of path and asset
    asset: str | None = None  # an asset id from /assets
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
    if isinstance(exc, ProjectNotFoundError | SourceNotFoundError | AssetNotFoundError):
        status = 404
    elif isinstance(exc, ProjectExistsError | OutputExistsError):
        status = 409
    elif isinstance(exc, OutsideRootError):
        status = 400
    else:
        status = 422
    return HTTPException(status_code=status, detail=str(exc))


def make_router(media: MediaStore, store: ProjectStore) -> APIRouter:
    router = APIRouter()
    jobs: dict[str, _RenderJob] = {}

    def clip_out(c: Clip, row: assets.Located | None) -> ClipOut:
        return ClipOut(
            id=c.id,
            asset=row.asset.id if row else c.asset,
            source=row.source if row else c.source,
            in_point=c.in_point,
            out_point=c.out_point,
            start=c.start,
            end=c.end,
            duration=c.duration,
            source_duration=row.asset.duration if row else None,
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

    def asset_out(book: assets.AssetBook, a: assets.Asset) -> AssetOut:
        return AssetOut(**a.summary(book.present(a)))

    def project_out(name: str, project: Project) -> ProjectOut:
        # One table load for the whole project; each clip's file is found by asset id.
        with assets.open_book(media) as book:
            rows = {
                c.id: assets.lookup(book, c.asset, c.source)
                for t in project.tracks
                for c in t.clips
                if c.kind == "media"
            }
        return ProjectOut(
            name=name,
            revision=project.revision,
            duration=project.duration,
            tracks=[
                TrackOut(
                    id=t.id, kind=t.kind, magnetic=t.magnetic, clips=[clip_out(c, rows.get(c.id)) for c in t.clips]
                )
                for t in project.tracks
            ],
        )

    def respond(name: str, action: Callable[[], Project]) -> ProjectOut:
        try:
            return project_out(name, action())
        except RevisionConflictError:
            raise  # the app's handler answers it: 409 with the current revision
        except TimelineError as exc:
            raise _http(exc) from None

    def asset_error(exc: Exception) -> HTTPException:
        if isinstance(exc, MediaOutsideRootError):
            return HTTPException(status_code=400, detail="path escapes the browse root")
        if isinstance(exc, ProbeError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=404, detail=str(exc))

    @router.get("/assets", response_model=AssetListOut)
    def list_assets() -> AssetListOut:
        """The workspace's asset table: every media file a timeline clip has used, by content-derived id."""
        with assets.open_book(media) as book:
            return AssetListOut(assets=[asset_out(book, a) for a in book.list()])

    @router.post("/assets/register", response_model=AssetOut)
    def register_asset(request: RegisterAssetRequest) -> AssetOut:
        """Add the file at `path` to the asset table (idempotent; the same bytes at another path are the same asset)."""
        try:
            with assets.open_book(media) as book:
                return asset_out(book, book.register(request.path))
        except (TimelineError, MediaError, ProbeError) as exc:
            raise asset_error(exc) from None

    @router.get("/assets/{asset_id}", response_model=AssetOut)
    def get_asset(asset_id: str) -> AssetOut:
        """One asset; if its file was renamed or moved it is found again and the table updated."""
        try:
            with assets.open_book(media) as book:
                book.require(asset_id)
                try:
                    row = book.locate(asset_id)
                except SourceNotFoundError:
                    row = book.require(asset_id)  # missing: still describe it, with present=false
                return asset_out(book, row)
        except TimelineError as exc:
            raise asset_error(exc) from None

    @router.get("/compose/projects", response_model=ProjectListOut)
    def list_projects() -> ProjectListOut:
        return ProjectListOut(projects=store.list_timelines())

    @router.post("/compose/projects", response_model=ProjectOut)
    def create_project(request: CreateProjectRequest) -> ProjectOut:
        return respond(request.name, lambda: timeline_service.create_project(store, request.name))

    @router.get("/compose/projects/{name}", response_model=ProjectOut)
    def get_project(name: str) -> ProjectOut:
        return respond(name, lambda: timeline_service.load_existing(store, name, media))

    @router.post("/compose/projects/{name}/clips", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def add_clip(name: str, request: AddClipRequest, base: int | None = Depends(base_revision)) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.add_clip(
                store,
                media,
                name,
                request.path,
                asset=request.asset,
                track_id=request.track_id,
                index=request.index,
                start=request.start,
                in_point=request.in_point,
                out_point=request.out_point,
                base_revision=base,
            ),
        )

    @router.patch("/compose/projects/{name}/clips/{clip_id}", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def trim_clip(
        name: str, clip_id: str, request: TrimRequest, base: int | None = Depends(base_revision)
    ) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.trim_clip(
                store, media, name, clip_id, in_point=request.in_point, out_point=request.out_point, base_revision=base
            ),
        )

    @router.post("/compose/projects/{name}/tracks", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def add_track(name: str, request: AddTrackRequest, base: int | None = Depends(base_revision)) -> ProjectOut:
        return respond(name, lambda: timeline_service.add_track(store, name, request.kind, media, base_revision=base))

    @router.delete("/compose/projects/{name}/tracks/{track_id}", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def remove_track(name: str, track_id: str, base: int | None = Depends(base_revision)) -> ProjectOut:
        return respond(name, lambda: timeline_service.remove_track(store, name, track_id, media, base_revision=base))

    @router.post(
        "/compose/projects/{name}/tracks/{track_id}/move", response_model=ProjectOut, responses=WRITE_RESPONSES
    )
    def move_track(
        name: str, track_id: str, request: MoveTrackRequest, base: int | None = Depends(base_revision)
    ) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.move_track(store, name, track_id, request.to_index, media, base_revision=base),
        )

    @router.patch(
        "/compose/projects/{name}/clips/{clip_id}/properties", response_model=ProjectOut, responses=WRITE_RESPONSES
    )
    def set_properties(
        name: str, clip_id: str, request: PropertiesRequest, base: int | None = Depends(base_revision)
    ) -> ProjectOut:
        changes = request.model_dump(exclude_none=True)
        return respond(
            name,
            lambda: timeline_service.set_clip_properties(
                store, name, clip_id, media=media, base_revision=base, **changes
            ),
        )

    @router.post(
        "/compose/projects/{name}/tracks/{track_id}/text", response_model=ProjectOut, responses=WRITE_RESPONSES
    )
    def add_text(
        name: str, track_id: str, request: AddTextRequest, base: int | None = Depends(base_revision)
    ) -> ProjectOut:
        options = request.model_dump(exclude={"text", "duration"})
        return respond(
            name,
            lambda: timeline_service.add_text_clip(
                store, name, track_id, request.text, request.duration, media=media, base_revision=base, **options
            ),
        )

    @router.patch("/compose/projects/{name}/clips/{clip_id}/text", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def set_text(
        name: str, clip_id: str, request: TextRequest, base: int | None = Depends(base_revision)
    ) -> ProjectOut:
        changes = request.model_dump(exclude_none=True)
        return respond(
            name,
            lambda: timeline_service.set_text_properties(
                store, name, clip_id, media=media, base_revision=base, **changes
            ),
        )

    @router.post("/compose/projects/{name}/clips/{clip_id}/place", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def place_clip(
        name: str, clip_id: str, request: PlaceRequest, base: int | None = Depends(base_revision)
    ) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.place_clip(
                store, name, clip_id, start=request.start, track_id=request.track_id, media=media, base_revision=base
            ),
        )

    @router.post("/compose/projects/{name}/clips/{clip_id}/move", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def move_clip(
        name: str, clip_id: str, request: MoveRequest, base: int | None = Depends(base_revision)
    ) -> ProjectOut:
        return respond(
            name, lambda: timeline_service.move_clip(store, name, clip_id, request.to_index, media, base_revision=base)
        )

    @router.delete("/compose/projects/{name}/clips/{clip_id}", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def remove_clip(name: str, clip_id: str, base: int | None = Depends(base_revision)) -> ProjectOut:
        return respond(name, lambda: timeline_service.remove_clip(store, name, clip_id, media, base_revision=base))

    @router.put(
        "/compose/projects/{name}/clips/{clip_id}/transition", response_model=ProjectOut, responses=WRITE_RESPONSES
    )
    def set_transition(
        name: str, clip_id: str, request: TransitionRequest, base: int | None = Depends(base_revision)
    ) -> ProjectOut:
        return respond(
            name,
            lambda: timeline_service.set_transition(
                store, name, clip_id, request.transition, request.duration, media, base_revision=base
            ),
        )

    @router.post("/compose/projects/{name}/render", response_model=RenderStartOut)
    def start_render(name: str, request: RenderRequest) -> RenderStartOut:
        try:
            project, filename, out_id = timeline_service.plan_render(
                store, media, name, request.output, request.overwrite
            )
        except TimelineError as exc:
            raise _http(exc) from None

        job = _RenderJob()
        job_id = uuid.uuid4().hex
        jobs[job_id] = job

        def run() -> None:
            def on_progress(fraction: float) -> None:
                job.progress = fraction

            try:
                timeline_service.run_render(project, media, out_id, on_progress)
                job.output = filename
                job.status = "done"
            except (TimelineError, ExtractError, ProbeError, MediaError, OSError) as exc:
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


def make_media_router(media: MediaStore) -> APIRouter:
    router = APIRouter()

    @router.get("/media/file")
    def media_file(path: str, request: Request) -> Response:
        """A video under the browse root, for the compose page's preview player."""
        try:
            media_id = media.resolve(path)
        except MediaOutsideRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root") from None
        if not media.is_file(media_id):
            raise HTTPException(status_code=404, detail="file not found")
        return media_response(media, media_id, request)

    @router.get("/media/asset/{asset_id}")
    def media_asset(asset_id: str, request: Request) -> Response:
        """The file of an asset (see /assets), found by id even if it was renamed since."""
        try:
            media_id = assets.media_id_of(media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return media_response(media, media_id, request)

    return router
