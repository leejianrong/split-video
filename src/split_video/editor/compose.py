"""HTTP API for the timeline ("compose") editor.

A separate router from the split editor's endpoints in `app.py`: it works on
timeline projects (`timeline.py`) stored in the browse root, not on the one
open video. Each request loads the project file, applies one operation from
`timeline_ops`, and saves it back, so the file on disk is always the truth
and the CLI/MCP can edit the same project.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import Response

from split_video import assets, ocr, proxy, scenes, timeline_ops, timeline_service, transcribe
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.job_routes import cancel_job, job_of_kind
from split_video.editor.media_response import media_response
from split_video.editor.revisions import WRITE_RESPONSES, base_revision
from split_video.errors import RevisionConflictError
from split_video.ffmpeg import ProbeError
from split_video.job_work import RENDER_STEPS, render_work
from split_video.jobs import RENDER, JobInfo, JobQueue
from split_video.media_store import MediaError, MediaOutsideRootError, MediaStore
from split_video.store_provider import StoreProvider
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


class CropBox(BaseModel):
    """A crop rectangle as fractions of the source frame, origin at its top-left."""

    x: float
    y: float
    w: float
    h: float


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
    crop: CropBox | None = None  # the part of the source frame shown; None = the full frame
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
    has_video: bool
    width: int | None
    height: int | None
    fps: float | None
    copies: list[str]  # other paths known to hold the same bytes
    present: bool  # whether the file is at `path` with this content right now
    proxy: bool = False  # a current proxy exists (see /assets/{id}/proxy)
    thumbnails: bool = False  # a current thumbnail strip exists (see /assets/{id}/thumbnails)
    transcript: bool = False  # a current transcript exists (see /assets/{id}/transcript)
    scenes: bool = False  # current scene-change and activity signals exist (see /assets/{id}/scenes)
    ocr: bool = False  # current on-screen text exists (see /assets/{id}/ocr)


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
    # The part of the source frame to show; `null` removes the crop. Absent = unchanged.
    crop: CropBox | None = None


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
    # Output frame size in pixels (both or neither; even, 64..4096). Default: the first clip's size.
    width: int | None = None
    height: int | None = None
    # Export preset name (GET /presets): sets size, frame rate and encoder. An explicit width and height beat the
    # preset's size; precedence is explicit > preset > the first clip's size.
    preset: str | None = None
    # How a picture whose aspect differs from the frame is fitted: "letterbox" (default) or "crop".
    fit: str | None = None


class RenderStartOut(BaseModel):
    job_id: str


class RenderStatusOut(BaseModel):
    status: str
    progress: float
    output: str | None = None
    error: str | None = None
    # What was used, once the render has finished (the same JSON as the CLI and MCP).
    name: str | None = None
    duration: float | None = None
    preset: str | None = None
    fit: str | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None


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


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

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
            crop=CropBox(**c.crop) if c.crop else None,
            kind=c.kind,
            text=c.text,
            font_size=c.font_size,
            color=c.color,
        )

    def asset_out(book: assets.AssetBook, a: assets.Asset) -> AssetOut:
        return AssetOut(
            **a.summary(book.present(a)),
            **proxy.flags(book.media, a),
            transcript=transcribe.has_transcript(book.media, a),
            scenes=scenes.has_scenes(book.media, a),
            ocr=ocr.has_ocr(book.media, a),
        )

    def project_out(media: MediaStore, name: str, project: Project) -> ProjectOut:
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

    def respond(ctx: RequestContext, name: str, action: Callable[[], Project]) -> ProjectOut:
        try:
            return project_out(ctx.media, name, action())
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
    def list_assets(ctx: RequestContext = ctx_dep) -> AssetListOut:
        """The workspace's asset table: every media file a timeline clip has used, by content-derived id."""
        with assets.open_book(ctx.media) as book:
            return AssetListOut(assets=[asset_out(book, a) for a in book.list()])

    @router.post("/assets/register", response_model=AssetOut)
    def register_asset(request: RegisterAssetRequest, ctx: RequestContext = ctx_dep) -> AssetOut:
        """Add the file at `path` to the asset table (idempotent; the same bytes at another path are the same asset)."""
        try:
            with assets.open_book(ctx.media) as book:
                return asset_out(book, book.register(request.path))
        except (TimelineError, MediaError, ProbeError) as exc:
            raise asset_error(exc) from None

    @router.get("/assets/{asset_id}/reframe", response_model=CropBox)
    def reframe_asset(
        asset_id: str,
        aspect: str = "9:16",
        center_x: float = 0.5,
        center_y: float = 0.5,
        zoom: float = 1.0,
        ctx: RequestContext = ctx_dep,
    ) -> CropBox:
        """The crop rectangle (fractions of the frame) of the largest `aspect` region of the asset's picture,
        centred on (`center_x`, `center_y`) and zoomed in by `zoom`: what to pass as a clip's `crop`."""
        try:
            with assets.open_book(ctx.media) as book:
                row = book.require(asset_id)
        except TimelineError as exc:
            raise asset_error(exc) from None
        try:
            if not row.width or not row.height:
                raise TimelineError(f"asset {asset_id} has no picture to crop")
            aspect_w, aspect_h = timeline_ops.parse_aspect(aspect)
            return CropBox(
                **timeline_ops.crop_for_aspect(row.width, row.height, aspect_w, aspect_h, center_x, center_y, zoom)
            )
        except TimelineError as exc:
            raise _http(exc) from None

    @router.get("/assets/{asset_id}", response_model=AssetOut)
    def get_asset(asset_id: str, ctx: RequestContext = ctx_dep) -> AssetOut:
        """One asset; if its file was renamed or moved it is found again and the table updated."""
        try:
            with assets.open_book(ctx.media) as book:
                book.require(asset_id)
                try:
                    row = book.locate(asset_id)
                except SourceNotFoundError:
                    row = book.require(asset_id)  # missing: still describe it, with present=false
                return asset_out(book, row)
        except TimelineError as exc:
            raise asset_error(exc) from None

    @router.get("/compose/projects", response_model=ProjectListOut)
    def list_projects(ctx: RequestContext = ctx_dep) -> ProjectListOut:
        return ProjectListOut(projects=ctx.store.list_timelines())

    @router.post("/compose/projects", response_model=ProjectOut)
    def create_project(request: CreateProjectRequest, ctx: RequestContext = ctx_dep) -> ProjectOut:
        return respond(ctx, request.name, lambda: timeline_service.create_project(ctx.store, request.name))

    @router.get("/compose/projects/{name}", response_model=ProjectOut)
    def get_project(name: str, ctx: RequestContext = ctx_dep) -> ProjectOut:
        return respond(ctx, name, lambda: timeline_service.load_existing(ctx.store, name, ctx.media))

    @router.post("/compose/projects/{name}/clips", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def add_clip(
        name: str, request: AddClipRequest, base: int | None = Depends(base_revision), ctx: RequestContext = ctx_dep
    ) -> ProjectOut:
        return respond(
            ctx,
            name,
            lambda: timeline_service.add_clip(
                ctx.store,
                ctx.media,
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
        name: str,
        clip_id: str,
        request: TrimRequest,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> ProjectOut:
        return respond(
            ctx,
            name,
            lambda: timeline_service.trim_clip(
                ctx.store,
                ctx.media,
                name,
                clip_id,
                in_point=request.in_point,
                out_point=request.out_point,
                base_revision=base,
            ),
        )

    @router.post("/compose/projects/{name}/tracks", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def add_track(
        name: str, request: AddTrackRequest, base: int | None = Depends(base_revision), ctx: RequestContext = ctx_dep
    ) -> ProjectOut:
        return respond(
            ctx, name, lambda: timeline_service.add_track(ctx.store, name, request.kind, ctx.media, base_revision=base)
        )

    @router.delete("/compose/projects/{name}/tracks/{track_id}", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def remove_track(
        name: str, track_id: str, base: int | None = Depends(base_revision), ctx: RequestContext = ctx_dep
    ) -> ProjectOut:
        return respond(
            ctx, name, lambda: timeline_service.remove_track(ctx.store, name, track_id, ctx.media, base_revision=base)
        )

    @router.post(
        "/compose/projects/{name}/tracks/{track_id}/move", response_model=ProjectOut, responses=WRITE_RESPONSES
    )
    def move_track(
        name: str,
        track_id: str,
        request: MoveTrackRequest,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> ProjectOut:
        return respond(
            ctx,
            name,
            lambda: timeline_service.move_track(
                ctx.store, name, track_id, request.to_index, ctx.media, base_revision=base
            ),
        )

    @router.patch(
        "/compose/projects/{name}/clips/{clip_id}/properties", response_model=ProjectOut, responses=WRITE_RESPONSES
    )
    def set_properties(
        name: str,
        clip_id: str,
        request: PropertiesRequest,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> ProjectOut:
        changes = request.model_dump(exclude_none=True, exclude={"crop"})
        if "crop" in request.model_fields_set:  # an explicit null removes the crop
            changes["crop"] = None if request.crop is None else request.crop.model_dump()
        return respond(
            ctx,
            name,
            lambda: timeline_service.set_clip_properties(
                ctx.store, name, clip_id, media=ctx.media, base_revision=base, **changes
            ),
        )

    @router.post(
        "/compose/projects/{name}/tracks/{track_id}/text", response_model=ProjectOut, responses=WRITE_RESPONSES
    )
    def add_text(
        name: str,
        track_id: str,
        request: AddTextRequest,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> ProjectOut:
        options = request.model_dump(exclude={"text", "duration"})
        return respond(
            ctx,
            name,
            lambda: timeline_service.add_text_clip(
                ctx.store,
                name,
                track_id,
                request.text,
                request.duration,
                media=ctx.media,
                base_revision=base,
                **options,
            ),
        )

    @router.patch("/compose/projects/{name}/clips/{clip_id}/text", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def set_text(
        name: str,
        clip_id: str,
        request: TextRequest,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> ProjectOut:
        changes = request.model_dump(exclude_none=True, exclude={"crop"})
        if "crop" in request.model_fields_set:  # an explicit null removes the crop
            changes["crop"] = None if request.crop is None else request.crop.model_dump()
        return respond(
            ctx,
            name,
            lambda: timeline_service.set_text_properties(
                ctx.store, name, clip_id, media=ctx.media, base_revision=base, **changes
            ),
        )

    @router.post("/compose/projects/{name}/clips/{clip_id}/place", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def place_clip(
        name: str,
        clip_id: str,
        request: PlaceRequest,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> ProjectOut:
        return respond(
            ctx,
            name,
            lambda: timeline_service.place_clip(
                ctx.store,
                name,
                clip_id,
                start=request.start,
                track_id=request.track_id,
                media=ctx.media,
                base_revision=base,
            ),
        )

    @router.post("/compose/projects/{name}/clips/{clip_id}/move", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def move_clip(
        name: str,
        clip_id: str,
        request: MoveRequest,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> ProjectOut:
        return respond(
            ctx,
            name,
            lambda: timeline_service.move_clip(
                ctx.store, name, clip_id, request.to_index, ctx.media, base_revision=base
            ),
        )

    @router.delete("/compose/projects/{name}/clips/{clip_id}", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def remove_clip(
        name: str, clip_id: str, base: int | None = Depends(base_revision), ctx: RequestContext = ctx_dep
    ) -> ProjectOut:
        return respond(
            ctx, name, lambda: timeline_service.remove_clip(ctx.store, name, clip_id, ctx.media, base_revision=base)
        )

    @router.put(
        "/compose/projects/{name}/clips/{clip_id}/transition", response_model=ProjectOut, responses=WRITE_RESPONSES
    )
    def set_transition(
        name: str,
        clip_id: str,
        request: TransitionRequest,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> ProjectOut:
        return respond(
            ctx,
            name,
            lambda: timeline_service.set_transition(
                ctx.store, name, clip_id, request.transition, request.duration, ctx.media, base_revision=base
            ),
        )

    @router.post("/compose/projects/{name}/render", response_model=RenderStartOut)
    def start_render(name: str, request: RenderRequest, ctx: RequestContext = ctx_dep) -> RenderStartOut:
        try:
            project, filename, out_id = timeline_service.plan_render(
                ctx.store,
                ctx.media,
                name,
                request.output,
                request.overwrite,
                request.width,
                request.height,
                request.preset,
                request.fit,
            )
        except TimelineError as exc:
            raise _http(exc) from None

        work = render_work(
            project, ctx.media, out_id, filename, request.width, request.height, request.preset, request.fit, name
        )
        job_id = jobs.submit(RENDER, work, label=filename, total=RENDER_STEPS, owner=ctx.principal.id)
        return RenderStartOut(job_id=job_id)

    def render_out(job: JobInfo) -> RenderStatusOut:
        summary = job.result or {}
        return RenderStatusOut(
            # The polling shape predates queued/cancelled: queued reads as running, and a cancelled
            # render reports status "cancelled" with error "cancelled".
            status="running" if job.state == "queued" else job.state,
            progress=job.fraction,
            output=summary.get("output"),
            name=summary.get("name"),
            duration=summary.get("duration"),
            preset=summary.get("preset"),
            fit=summary.get("fit"),
            width=summary.get("width"),
            height=summary.get("height"),
            fps=summary.get("fps"),
            error="cancelled" if job.state == "cancelled" else job.error,
        )

    @router.get("/compose/render/{job_id}", response_model=RenderStatusOut)
    def render_status(job_id: str, ctx: RequestContext = ctx_dep) -> RenderStatusOut:
        return render_out(job_of_kind(jobs, job_id, RENDER, ctx.principal))

    @router.post("/compose/render/{job_id}/cancel", response_model=RenderStatusOut)
    def render_cancel(job_id: str, ctx: RequestContext = ctx_dep) -> RenderStatusOut:
        """Stop a render: ffmpeg is terminated and the partial output file deleted (the same as
        `POST /jobs/{job_id}/cancel`, answering in the polling shape)."""
        return render_out(cancel_job(jobs, job_id, RENDER, ctx.principal))

    return router


def make_media_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    @router.get("/media/file")
    def media_file(path: str, request: Request, ctx: RequestContext = ctx_dep) -> Response:
        """A video under the browse root, for the compose page's preview player."""
        try:
            media_id = ctx.media.resolve(path)
        except MediaOutsideRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root") from None
        if not ctx.media.is_file(media_id):
            raise HTTPException(status_code=404, detail="file not found")
        return media_response(ctx.media, media_id, request)

    @router.get("/media/asset/{asset_id}")
    def media_asset(asset_id: str, request: Request, ctx: RequestContext = ctx_dep) -> Response:
        """The file of an asset (see /assets), found by id even if it was renamed since."""
        try:
            media_id = assets.media_id_of(ctx.media, asset_id)
        except (TimelineError, MediaError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return media_response(ctx.media, media_id, request)

    return router
