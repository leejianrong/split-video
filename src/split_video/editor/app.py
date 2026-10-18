"""FastAPI app for the browser-based split-point editor."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from split_video.editor.auth import (
    SECURITY_DESCRIPTION,
    SECURITY_SCHEME,
    UNAUTHORIZED_RESPONSES,
    Authenticator,
    AuthMiddleware,
    Unauthorized,
    authenticator_from_env,
    current_principal,
    principal_dependency,
    unauthorized_response,
)
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.editor.classify import ClassificationRegion
from split_video.editor.compose import make_media_router as make_compose_media_router
from split_video.editor.compose import make_router as make_compose_router
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.events import EventBus, EventsConfig
from split_video.editor.events import make_router as make_events_router
from split_video.editor.job_routes import cancel_job, job_of_kind
from split_video.editor.job_routes import make_router as make_jobs_router
from split_video.editor.media_response import media_response
from split_video.editor.revisions import WRITE_RESPONSES, base_revision, conflict_response
from split_video.editor.schemas import (
    PROJECT_PATH_DESCRIPTION,
    AnalyzeStartResponse,
    AnalyzeStatusResponse,
    BrowseEntryOut,
    BrowseResponse,
    ClassificationRegionOut,
    ClassificationResponse,
    DetectRequest,
    DetectResponse,
    ExportRequest,
    ExportStartResponse,
    ExportStatusResponse,
    OpenRequest,
    ProjectSaveRequest,
    RethresholdRequest,
    SegmentOut,
    SegmentsRequest,
    SegmentsResponse,
    SessionResponse,
    SilenceIntervalOut,
    StateParams,
    StateResponse,
    WaveformResponse,
    WhoAmIOut,
)
from split_video.errors import RevisionConflictError
from split_video.identity import LOCAL_PRINCIPAL, Principal
from split_video.job_work import analysis_work, export_work
from split_video.jobs import ANALYSIS, EXPORT, InProcessJobQueue, JobInfo, JobQueue
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import LocalProjectStore
from split_video.media_store import MediaNotFoundError, MediaOutsideRootError, MediaStore
from split_video.project import ExportConflictError, ProjectError, plan_export
from split_video.segments import Segment, compute_segments
from split_video.silence import SilenceInterval
from split_video.store import ProjectStore, SavedSegment
from split_video.store_provider import SharedStoreProvider, StoreProvider

STATIC_DIR = Path(__file__).parent / "static"

# Every JSON/media route lives under this prefix; it is the documented,
# versioned surface (see openapi.json at the repo root).
API_PREFIX = "/api/v1"


_LEGACY_MEDIA_PATHS = ("/media/source", "/media/file")


def _is_legacy_path(path: str) -> bool:
    """A pre-versioning spelling kept as a deprecated alias for one release."""
    if path in _LEGACY_MEDIA_PATHS or path.startswith("/media/asset/"):
        return True
    return path.startswith("/api/") and not path.startswith(f"{API_PREFIX}/")


@dataclass
class _ProjectState:
    """Everything the editor holds in memory for one source video.

    One instance per resolved source path, owned by the app's project
    registry (see `create_app`); nothing here is shared between projects.
    """

    source: str  # the video's media id in the `MediaStore`
    # The project's id in the `ProjectStore`: the same id (the video's path relative to the browse root).
    project_id: str
    total_duration: float
    cache: SilenceCache
    waveform_cache: WaveformCache
    classification_cache: ClassificationCache
    # The saved segments and revision are not held here: the project file is the truth (the CLI and
    # MCP server edit it too), so every state response reads it afresh from the store.
    lock: threading.Lock = field(default_factory=threading.Lock)


def _build_project_state(source: str, project_id: str, media: MediaStore) -> _ProjectState:
    return _ProjectState(
        source=source,
        project_id=project_id,
        total_duration=media.probe(source).duration,
        cache=SilenceCache(media, source),
        waveform_cache=WaveformCache(media, source),
        classification_cache=ClassificationCache(media, source),
    )


def _legacy_status(info: JobInfo) -> str:
    """The `status` the per-kind polling endpoints have always reported: `queued` reads as `running`
    (the job has been accepted and is not finished); `cancelled` is the one new value."""
    return "running" if info.state == "queued" else info.state


def _legacy_error(info: JobInfo) -> str | None:
    return "cancelled" if info.state == "cancelled" else info.error


def create_app(
    root: Path,
    defaults: StateParams,
    store: ProjectStore | None = None,
    media: MediaStore | None = None,
    jobs: JobQueue | None = None,
    events: EventsConfig | None = None,
    provider: StoreProvider | None = None,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    """`root` is either a video file to open immediately (the historical
    `split-video edit <file>` behavior, unchanged) or a directory to pick a
    video from via the in-browser file picker. Projects are read and written
    through `store` and media (sources, outputs, caches) through `media`; by default
    both are local-disk stores rooted at the browse root. Export, analysis and render run as jobs on
    `jobs`, by default an in-process thread-pool queue owned (and shut down) by the app. `events` tunes the
    `/api/v1/events` stream (poll, keepalive and throttle intervals); the bus is `app.state.events`.

    Every request is made by a `Principal`, decided by `authenticator` (default: from the environment, see
    `editor/auth.py`: local single-user mode with no checks unless `SPLIT_VIDEO_TOKEN` is set). Routes get
    their stores from `provider` for that principal; `store`/`media` are the shorthand for a provider that
    returns the same two stores to everyone (give one or the other, not both)."""
    # `version` is the API version, not the package version, so the committed
    # openapi.json only changes when the API does.
    own_jobs = jobs is None
    projects: dict[tuple[str, str], _ProjectState] = {}  # (principal id, source media id)
    projects_lock = threading.Lock()
    jobs = InProcessJobQueue() if jobs is None else jobs

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        bus.close()  # end open event streams (the server's own shutdown hook does this earlier, see serve.py)
        if own_jobs and isinstance(jobs, InProcessJobQueue):
            jobs.shutdown()  # stop running ffmpeg processes with the server

    app = FastAPI(title="split-video editor", version="1", lifespan=lifespan)
    authenticator = authenticator_from_env() if authenticator is None else authenticator
    app.state.authenticator = authenticator

    @app.exception_handler(Unauthorized)
    async def unauthorized(request: Request, exc: Unauthorized) -> Response:
        return unauthorized_response()

    # Under /api/ and /media/ nothing is routed before the caller is authenticated (a no-op in local mode).
    app.add_middleware(AuthMiddleware, authenticator=authenticator)

    @app.exception_handler(RevisionConflictError)
    async def revision_conflict(request: Request, exc: RevisionConflictError) -> Response:
        return conflict_response(exc)

    # Routes are declared once on these two routers (no /api prefix, no
    # /media prefix) and mounted twice at the bottom: under /api/v1 (the
    # schema) and as deprecated /api and /media aliases (hidden from it).
    api = APIRouter()
    media_routes = APIRouter()

    # The static frontend (index.html/*.js/*.css, mounted at the bottom of
    # this function) has no cache-busting — no content hash or version
    # query string in its URLs — so absent an explicit header, a browser's
    # own heuristic caching can keep serving an old index.html or main.js
    # for a tab that's had this origin open across a rebuild. Since that
    # rebuild might have renamed or removed a DOM id, the result is a
    # stale script reaching for an element that no longer exists — a
    # `Cannot read properties of null` crash that looks like a real bug in
    # whatever shipped, but is actually just a mismatched old/new asset
    # pair. This is a local single-user tool on localhost; there's no
    # meaningful cost to never caching these, so don't.
    @app.middleware("http")
    async def no_store_for_static_assets(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if not path.startswith(("/api/", "/media/")):
            response.headers["Cache-Control"] = "no-store"
        if _is_legacy_path(path):
            response.headers["Deprecation"] = "true"
            response.headers["Link"] = f'<{API_PREFIX}>; rel="successor-version"'
        return response

    browse_root = root.parent if root.is_file() else root
    if provider is not None and (store is not None or media is not None):
        raise ValueError("give either provider or store/media, not both")
    if provider is None:
        provider = SharedStoreProvider(
            store if store is not None else LocalProjectStore(browse_root.resolve()),
            media if media is not None else LocalMediaStore(browse_root.resolve()),
        )
    app.state.stores = provider
    request_context = make_context_dependency(provider)
    context_dependency = Depends(request_context)
    # Registry of per-project state, keyed by canonical source media id and built
    # lazily on first use. The project id a client sends is the video's path
    # relative to the browse root; there is no "currently open" file.
    default_project_id = root.name if root.is_file() else None

    def _open_project_ids() -> list[str]:
        with projects_lock:
            return [p.project_id for p in projects.values()]

    # The bus watches one store: the local principal's. Project events of a per-principal provider's other
    # stores are not reported (a documented limitation; project events carry no owner either).
    bus = EventBus(jobs, provider.project_store(LOCAL_PRINCIPAL), _open_project_ids, events)
    app.state.events = bus

    def _segments_out(segments: list[SavedSegment]) -> list[SegmentOut]:
        return [
            SegmentOut(
                index=i + 1,
                start=s.start,
                end=s.end,
                duration=s.end - s.start,
                label=s.label,
                color=s.color,
                included=s.included,
                export_name=s.export_name,
            )
            for i, s in enumerate(segments)
        ]

    def _to_saved(segments: list[Segment]) -> list[SavedSegment]:
        """Bare core `Segment`s (e.g. fresh silence detection) as
        `SavedSegment`s with default (unset) editor metadata."""
        return [SavedSegment(start=s.start, end=s.end) for s in segments]

    def _silences_out(silences: list[SilenceInterval]) -> list[SilenceIntervalOut]:
        return [SilenceIntervalOut(start=s.start, end=s.end) for s in silences]

    def _resolve(media: MediaStore, path: str) -> str:
        try:
            media_id = media.resolve(path)
        except MediaOutsideRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root")
        if not media.is_file(media_id):
            raise HTTPException(status_code=404, detail="file not found")
        return media_id

    def _get_project(ctx: RequestContext, path: str) -> _ProjectState:
        """The state for project `path`, built on first use (probing and the caches)."""
        media = ctx.media
        source = _resolve(media, path)
        key = (ctx.principal.id, source)
        with projects_lock:
            project = projects.get(key)
        if project is None:
            # The id is the canonical (symlink-resolved) source id,
            # so a symlinked video keeps its project beside the real file.
            built = _build_project_state(source, source, media)  # outside the lock: probes with ffmpeg
            with projects_lock:
                project = projects.setdefault(key, built)
        return project

    def project_dep(
        path: str = Query(..., description=PROJECT_PATH_DESCRIPTION), ctx: RequestContext = context_dependency
    ) -> _ProjectState:
        return _get_project(ctx, path)

    project_dependency = Depends(project_dep)

    def _state_response(ctx: RequestContext, project: _ProjectState, path: str) -> StateResponse:
        store, media = ctx.store, ctx.media
        # A never-saved project has no segments: the frontend's onboarding step proposes them (#16).
        saved = store.load_split_versioned(project.project_id)
        return StateResponse(
            filename=media.name(project.source),
            duration=project.total_duration,
            video_url=f"{API_PREFIX}/media/source?path={quote(path, safe='/')}",
            params=defaults,
            silences=[],
            segments=_segments_out(saved.value or []),
            resumed=saved.value is not None,
            revision=saved.revision,
        )

    @api.get("/session", response_model=SessionResponse)
    def get_session() -> SessionResponse:
        # No per-server "open file": this only tells the frontend which file
        # (if any) the server was started on, so it can open it directly.
        return SessionResponse(
            file_open=default_project_id is not None,
            filename=default_project_id,
            path=default_project_id,
        )

    @api.get("/browse", response_model=BrowseResponse)
    def browse(path: str = "", ctx: RequestContext = context_dependency) -> BrowseResponse:
        try:
            listing = ctx.media.list_dir(path)
        except MediaOutsideRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root")
        except MediaNotFoundError:
            raise HTTPException(status_code=404, detail="directory not found")
        return BrowseResponse(
            cwd=listing.cwd,
            parent=listing.parent,
            entries=[BrowseEntryOut(name=e.name, path=e.path, is_dir=e.is_dir) for e in listing.entries],
        )

    @api.post("/open", response_model=StateResponse)
    def open_file(request: OpenRequest, ctx: RequestContext = context_dependency) -> StateResponse:
        return _state_response(ctx, _get_project(ctx, request.path), request.path)

    @api.get("/state", response_model=StateResponse)
    def get_state(
        path: str = Query(..., description=PROJECT_PATH_DESCRIPTION), ctx: RequestContext = context_dependency
    ) -> StateResponse:
        return _state_response(ctx, _get_project(ctx, path), path)

    @api.post("/project", response_model=StateResponse, responses=WRITE_RESPONSES)
    def save_current_project(
        request: ProjectSaveRequest,
        path: str = Query(..., description=PROJECT_PATH_DESCRIPTION),
        base: int | None = Depends(base_revision),
        ctx: RequestContext = context_dependency,
    ) -> StateResponse:
        """Replace the project's segments. Send `If-Match` with the revision they were edited from;
        409 `revision_conflict` if the project changed since (the response says its current revision)."""
        project = _get_project(ctx, path)
        segments = [
            SavedSegment(
                start=s.start,
                end=s.end,
                label=s.label,
                color=s.color,
                included=s.included,
                export_name=s.export_name,
            )
            for s in request.segments
        ]
        ctx.store.save_split(project.project_id, segments, base)
        return _state_response(ctx, project, path)

    @media_routes.get("/media/source")
    def get_media(
        request: Request, project: _ProjectState = project_dependency, ctx: RequestContext = context_dependency
    ) -> Response:
        return media_response(ctx.media, project.source, request)

    @api.post("/detect", response_model=DetectResponse)
    def detect(request: DetectRequest, project: _ProjectState = project_dependency) -> DetectResponse:
        silences = project.cache.get_raw_silences(request.silence_threshold)
        return DetectResponse(silences=_silences_out(silences))

    @api.get("/waveform", response_model=WaveformResponse)
    def waveform(project: _ProjectState = project_dependency) -> WaveformResponse:
        peaks = project.waveform_cache.get_peaks()
        return WaveformResponse(buckets=peaks.buckets)

    def _region_out(region: ClassificationRegion) -> ClassificationRegionOut:
        return ClassificationRegionOut(
            start=region.start,
            end=region.end,
            bucket=region.bucket,
            score=region.score,
            secondary=region.secondary,
            scores=region.bucket_scores,
        )

    def _classification_out(cache: ClassificationCache, analyzed: bool) -> ClassificationResponse:
        return ClassificationResponse(
            analyzed=analyzed,
            regions=[_region_out(r) for r in cache.get_regions()],
            lanes={bucket: [_region_out(r) for r in regions] for bucket, regions in cache.get_lanes().items()},
            thresholds=cache.get_thresholds(),
        )

    @api.post("/analyze", response_model=AnalyzeStartResponse)
    def analyze(
        project: _ProjectState = project_dependency, ctx: RequestContext = context_dependency
    ) -> AnalyzeStartResponse:
        media, owner = ctx.media, ctx.principal.id
        cache = project.classification_cache
        if cache.is_analyzed:
            # Already have a result — this session, or loaded from a
            # sidecar cache on disk. Report a job that's already "done"
            # instead of re-decoding audio and rerunning inference.
            return AnalyzeStartResponse(
                job_id=jobs.record_done(ANALYSIS, label=media.name(project.source), owner=owner)
            )
        work = analysis_work(media, project.source, cache, cache.get_thresholds())
        return AnalyzeStartResponse(job_id=jobs.submit(ANALYSIS, work, label=media.name(project.source), owner=owner))

    @api.get("/analyze/{job_id}", response_model=AnalyzeStatusResponse)
    def analyze_status(job_id: str, ctx: RequestContext = context_dependency) -> AnalyzeStatusResponse:
        job = job_of_kind(jobs, job_id, ANALYSIS, ctx.principal)
        return AnalyzeStatusResponse(
            status=_legacy_status(job), completed=int(job.completed), total=int(job.total), error=_legacy_error(job)
        )

    @api.post("/analyze/{job_id}/cancel", response_model=AnalyzeStatusResponse)
    def analyze_cancel(job_id: str, ctx: RequestContext = context_dependency) -> AnalyzeStatusResponse:
        """Stop an analysis job (the same as `POST /jobs/{job_id}/cancel`, answering in the polling shape)."""
        job = cancel_job(jobs, job_id, ANALYSIS, ctx.principal)
        return AnalyzeStatusResponse(
            status=_legacy_status(job), completed=int(job.completed), total=int(job.total), error=_legacy_error(job)
        )

    @api.get("/classification", response_model=ClassificationResponse)
    def classification(project: _ProjectState = project_dependency) -> ClassificationResponse:
        cache = project.classification_cache
        return _classification_out(cache, cache.is_analyzed)

    @api.post("/classification/thresholds", response_model=ClassificationResponse)
    def rethreshold_classification(
        request: RethresholdRequest, project: _ProjectState = project_dependency
    ) -> ClassificationResponse:
        cache = project.classification_cache
        try:
            cache.rethreshold(request.thresholds)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return _classification_out(cache, True)

    @api.post("/segments", response_model=SegmentsResponse)
    def segments_endpoint(request: SegmentsRequest) -> SegmentsResponse:
        silences = [SilenceInterval(start=s.start, end=s.end) for s in request.silences]
        computed = compute_segments(
            silences,
            request.duration,
            request.min_silence_duration,
            request.min_song_length,
            request.padding,
        )
        return SegmentsResponse(segments=_segments_out(_to_saved(computed)))

    @api.post("/export", response_model=ExportStartResponse)
    def export(
        request: ExportRequest, project: _ProjectState = project_dependency, ctx: RequestContext = context_dependency
    ) -> ExportStartResponse:
        media = ctx.media
        try:
            plan = plan_export(
                media,
                project.source,
                [(s.start, s.end, s.name) for s in request.segments],
                output_format=request.output_format,
                overwrite=request.overwrite,
            )
        except ExportConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ProjectError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        work = export_work(
            media,
            plan,
            precise=request.precise,
            output_format=request.output_format,
            write_manifest_file=request.manifest,
            parameters={
                "min_silence_duration": defaults.min_silence_duration,
                "min_song_length": defaults.min_song_length,
                "silence_padding": defaults.padding,
                "precise": request.precise,
            },
        )
        job_id = jobs.submit(
            EXPORT, work, label=media.name(project.source), total=len(plan.segments), owner=ctx.principal.id
        )
        return ExportStartResponse(job_id=job_id)

    def _export_status(job: JobInfo) -> ExportStatusResponse:
        summary = job.result or {}
        return ExportStatusResponse(
            status=_legacy_status(job),
            completed=int(job.completed),
            total=int(job.total),
            current_file=job.message,
            output_dir=summary.get("output_dir"),
            manifest_path=summary.get("manifest_path"),
            files=list(summary.get("files", [])),
            error=_legacy_error(job),
        )

    @api.get("/export/{job_id}", response_model=ExportStatusResponse)
    def export_status(job_id: str, ctx: RequestContext = context_dependency) -> ExportStatusResponse:
        return _export_status(job_of_kind(jobs, job_id, EXPORT, ctx.principal))

    @api.post("/export/{job_id}/cancel", response_model=ExportStatusResponse)
    def export_cancel(job_id: str, ctx: RequestContext = context_dependency) -> ExportStatusResponse:
        """Stop an export (the same as `POST /jobs/{job_id}/cancel`): files already written stay, the one in progress is removed."""
        return _export_status(cancel_job(jobs, job_id, EXPORT, ctx.principal))

    api.include_router(make_jobs_router(jobs))
    api.include_router(make_compose_router(provider, jobs))
    media_routes.include_router(make_compose_media_router(provider))

    # New in v1, no deprecated alias.
    v1_only = APIRouter()

    @v1_only.get("/whoami", response_model=WhoAmIOut)
    def whoami(principal: Principal = principal_dependency) -> WhoAmIOut:
        """The principal this request is made as; use it to check that a token works."""
        return WhoAmIOut(
            id=principal.id, name=principal.name, kind=principal.kind, auth_required=authenticator.required
        )

    # Every router goes in with the principal dependency (belt and braces with `AuthMiddleware`) and the
    # documented 401, so a route added to any of them is protected without doing anything.
    guard: dict = {"dependencies": [Depends(current_principal)], "responses": UNAUTHORIZED_RESPONSES}
    app.include_router(api, prefix=API_PREFIX, **guard)
    app.include_router(media_routes, prefix=API_PREFIX, **guard)
    app.include_router(v1_only, prefix=API_PREFIX, **guard)
    # The event stream is new in v1 and has no deprecated alias: it is registered only under /api/v1.
    app.include_router(make_events_router(bus), prefix=API_PREFIX, **guard)
    # Deprecated aliases for one release: /api/... and /media/... as they were
    # before versioning. Not in the OpenAPI schema; responses carry a
    # `Deprecation` header (set by the middleware above). Registered after /api/v1 so it can't shadow it.
    app.include_router(api, prefix="/api", include_in_schema=False, **guard)
    app.include_router(media_routes, include_in_schema=False, **guard)

    default_openapi = app.openapi

    def openapi() -> dict:
        if app.openapi_schema is None:
            schema = default_openapi()
            schema.setdefault("components", {}).setdefault("securitySchemes", {})[SECURITY_SCHEME] = {
                "type": "http",
                "scheme": "bearer",
                "description": SECURITY_DESCRIPTION,
            }
            schema["security"] = [{SECURITY_SCHEME: []}]
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]

    # Mounted last: Starlette matches routes in registration order, and this
    # catch-all static mount must not shadow the /api/* and /media/* routes.
    # The static/ directory is owned by the frontend build; if it's not
    # populated yet, drop a placeholder so the mount doesn't error.
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    if not any(STATIC_DIR.iterdir()):
        (STATIC_DIR / "index.html").write_text("<!doctype html><title>split-video editor</title><p>loading...</p>")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
