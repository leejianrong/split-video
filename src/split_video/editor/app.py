"""FastAPI app for the browser-based split-point editor."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from split_video.editor.browse import (
    PathEscapesRootError,
    list_directory,
    resolve_within_root,
)
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.editor.classify import ClassificationRegion
from split_video.editor.compose import make_router as make_compose_router
from split_video.editor.jobs import (
    AnalysisJobStore,
    JobStore,
    start_audio_analysis,
    start_export,
)
from split_video.editor.project_store import SavedSegment
from split_video.editor.project_store import load as load_project
from split_video.editor.project_store import save as save_project
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
)
from split_video.ffmpeg import probe_duration
from split_video.project import ExportConflictError, ProjectError, plan_export
from split_video.segments import Segment, compute_segments
from split_video.silence import SilenceInterval

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class _ProjectState:
    """Everything the editor holds in memory for one source video.

    One instance per resolved source path, owned by the app's project
    registry (see `create_app`); nothing here is shared between projects.
    """

    source: Path
    total_duration: float
    cache: SilenceCache
    waveform_cache: WaveformCache
    classification_cache: ClassificationCache
    # SavedSegment (not the plain core `Segment`) since these carry the
    # editor-only label/color/included/export_name metadata alongside
    # start/end — see project_store.py. A never-saved file starts empty; the
    # frontend's onboarding step is what proposes splits (see #16).
    initial_segments: list[SavedSegment]
    # Whether `initial_segments` came from a saved project file (see #14).
    resumed: bool
    lock: threading.Lock = field(default_factory=threading.Lock)


def _build_project_state(source: Path) -> _ProjectState:
    saved = load_project(source)
    return _ProjectState(
        source=source,
        total_duration=probe_duration(source),
        cache=SilenceCache(source),
        waveform_cache=WaveformCache(source),
        classification_cache=ClassificationCache(source),
        initial_segments=saved if saved is not None else [],
        resumed=saved is not None,
    )


def create_app(root: Path, defaults: StateParams) -> FastAPI:
    """`root` is either a video file to open immediately (the historical
    `split-video edit <file>` behavior, unchanged) or a directory to pick a
    video from via the in-browser file picker."""
    app = FastAPI(title="split-video editor")

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
        if not request.url.path.startswith(("/api/", "/media/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    browse_root = root.parent if root.is_file() else root
    job_store = JobStore()
    analysis_job_store = AnalysisJobStore()
    # Registry of per-project state, keyed by resolved source path and built
    # lazily on first use. The project id a client sends is the video's path
    # relative to the browse root; there is no "currently open" file.
    projects: dict[Path, _ProjectState] = {}
    projects_lock = threading.Lock()
    default_project_path = root.name if root.is_file() else None

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

    def _resolve(path: str) -> Path:
        try:
            resolved = resolve_within_root(browse_root, path)
        except PathEscapesRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root")
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return resolved

    def _get_project(path: str, refresh: bool = False) -> _ProjectState:
        """The state for project `path`, built on first use. `refresh`
        re-reads the saved project sidecar (what `/api/open` means) while
        keeping the already-built caches."""
        source = _resolve(path)
        with projects_lock:
            project = projects.get(source)
        if project is None:
            built = _build_project_state(source)  # outside the lock: probes with ffmpeg
            with projects_lock:
                project = projects.setdefault(source, built)
        if refresh:
            saved = load_project(source)
            with project.lock:
                project.initial_segments = saved if saved is not None else []
                project.resumed = saved is not None
        return project

    def project_dep(path: str = Query(..., description=PROJECT_PATH_DESCRIPTION)) -> _ProjectState:
        return _get_project(path)

    project_dependency = Depends(project_dep)

    def _state_response(project: _ProjectState, path: str) -> StateResponse:
        with project.lock:
            return StateResponse(
                filename=project.source.name,
                duration=project.total_duration,
                video_url=f"/media/source?path={quote(path, safe='/')}",
                params=defaults,
                silences=[],
                segments=_segments_out(project.initial_segments),
                resumed=project.resumed,
            )

    @app.get("/api/session", response_model=SessionResponse)
    def get_session() -> SessionResponse:
        # No per-server "open file": this only tells the frontend which file
        # (if any) the server was started on, so it can open it directly.
        return SessionResponse(
            file_open=default_project_path is not None,
            filename=default_project_path,
            path=default_project_path,
        )

    @app.get("/api/browse", response_model=BrowseResponse)
    def browse(path: str = "") -> BrowseResponse:
        try:
            listing = list_directory(browse_root, path)
        except PathEscapesRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root")
        except (FileNotFoundError, NotADirectoryError):
            raise HTTPException(status_code=404, detail="directory not found")
        return BrowseResponse(
            cwd=listing.cwd,
            parent=listing.parent,
            entries=[BrowseEntryOut(name=e.name, path=e.path, is_dir=e.is_dir) for e in listing.entries],
        )

    @app.post("/api/open", response_model=StateResponse)
    def open_file(request: OpenRequest) -> StateResponse:
        return _state_response(_get_project(request.path, refresh=True), request.path)

    @app.get("/api/state", response_model=StateResponse)
    def get_state(path: str = Query(..., description=PROJECT_PATH_DESCRIPTION)) -> StateResponse:
        return _state_response(_get_project(path), path)

    @app.post("/api/project", response_model=StateResponse)
    def save_current_project(
        request: ProjectSaveRequest, path: str = Query(..., description=PROJECT_PATH_DESCRIPTION)
    ) -> StateResponse:
        project = _get_project(path)
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
        save_project(project.source, segments)
        with project.lock:
            project.initial_segments = segments
            project.resumed = True
        return _state_response(project, path)

    @app.get("/media/source")
    def get_media(project: _ProjectState = project_dependency) -> FileResponse:
        return FileResponse(project.source)

    @app.post("/api/detect", response_model=DetectResponse)
    def detect(request: DetectRequest, project: _ProjectState = project_dependency) -> DetectResponse:
        silences = project.cache.get_raw_silences(request.silence_threshold)
        return DetectResponse(silences=_silences_out(silences))

    @app.get("/api/waveform", response_model=WaveformResponse)
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

    @app.post("/api/analyze", response_model=AnalyzeStartResponse)
    def analyze(project: _ProjectState = project_dependency) -> AnalyzeStartResponse:
        cache = project.classification_cache
        if cache.is_analyzed:
            # Already have a result — this session, or loaded from a
            # sidecar cache on disk. Report a job that's already "done"
            # instead of re-decoding audio and rerunning inference.
            job_id, _ = analysis_job_store.create_done(total=0)
            return AnalyzeStartResponse(job_id=job_id)
        thresholds = cache.get_thresholds()
        job_id = start_audio_analysis(analysis_job_store, project.source, cache, thresholds)
        return AnalyzeStartResponse(job_id=job_id)

    @app.get("/api/analyze/{job_id}", response_model=AnalyzeStatusResponse)
    def analyze_status(job_id: str) -> AnalyzeStatusResponse:
        job = analysis_job_store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job id")
        with job.lock:
            return AnalyzeStatusResponse(status=job.status, completed=job.completed, total=job.total, error=job.error)

    @app.get("/api/classification", response_model=ClassificationResponse)
    def classification(project: _ProjectState = project_dependency) -> ClassificationResponse:
        cache = project.classification_cache
        return _classification_out(cache, cache.is_analyzed)

    @app.post("/api/classification/thresholds", response_model=ClassificationResponse)
    def rethreshold_classification(
        request: RethresholdRequest, project: _ProjectState = project_dependency
    ) -> ClassificationResponse:
        cache = project.classification_cache
        try:
            cache.rethreshold(request.thresholds)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return _classification_out(cache, True)

    @app.post("/api/segments", response_model=SegmentsResponse)
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

    @app.post("/api/export", response_model=ExportStartResponse)
    def export(request: ExportRequest, project: _ProjectState = project_dependency) -> ExportStartResponse:
        try:
            plan = plan_export(
                project.source,
                [(s.start, s.end, s.name) for s in request.segments],
                output_format=request.output_format,
                overwrite=request.overwrite,
            )
        except ExportConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ProjectError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        job_id = start_export(
            job_store,
            plan,
            request.precise,
            request.output_format,
            request.manifest,
            parameters={
                "min_silence_duration": defaults.min_silence_duration,
                "min_song_length": defaults.min_song_length,
                "silence_padding": defaults.padding,
                "precise": request.precise,
            },
        )
        return ExportStartResponse(job_id=job_id)

    @app.get("/api/export/{job_id}", response_model=ExportStatusResponse)
    def export_status(job_id: str) -> ExportStatusResponse:
        job = job_store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job id")
        with job.lock:
            return ExportStatusResponse(
                status=job.status,
                completed=job.completed,
                total=job.total,
                current_file=job.current_file,
                output_dir=job.output_dir,
                manifest_path=job.manifest_path,
                files=list(job.files),
                error=job.error,
            )

    app.include_router(make_compose_router(browse_root))

    # Mounted last: Starlette matches routes in registration order, and this
    # catch-all static mount must not shadow the /api/* and /media/* routes.
    # The static/ directory is owned by the frontend build; if it's not
    # populated yet, drop a placeholder so the mount doesn't error.
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    if not any(STATIC_DIR.iterdir()):
        (STATIC_DIR / "index.html").write_text("<!doctype html><title>split-video editor</title><p>loading...</p>")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
