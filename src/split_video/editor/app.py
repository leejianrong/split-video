"""FastAPI app for the browser-based split-point editor."""

from __future__ import annotations

import itertools
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from split_video.editor.browse import (
    PathEscapesRootError,
    list_directory,
    resolve_within_root,
)
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.editor.classify import ClassificationRegion
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
from split_video.naming import resolve_export_filename
from split_video.segments import Segment, compute_segments
from split_video.silence import SilenceInterval

STATIC_DIR = Path(__file__).parent / "static"


class _Session:
    """The one file currently open for editing, if any.

    A local single-user tool only ever edits one file at a time, so this is
    deliberately just mutable state on the app rather than a per-client
    session store — `/api/open` swaps it out, everything else reads it.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.source: Path | None = None
        self.total_duration: float = 0.0
        self.cache: SilenceCache | None = None
        self.waveform_cache: WaveformCache | None = None
        self.classification_cache: ClassificationCache | None = None
        self.initial_silences: list[SilenceInterval] = []
        # SavedSegment (not the plain core `Segment`) since these carry the
        # editor-only label/color/included/export_name metadata from #19/#18
        # alongside start/end — see project_store.py.
        self.initial_segments: list[SavedSegment] = []
        # The initial silence scan (see `_open`) runs on a background thread
        # so opening a long recording can't block the HTTP server itself
        # from accepting connections — see #16: pointing `edit` straight at
        # a multi-hour file used to leave nothing listening for however long
        # that ffmpeg pass took, which looks exactly like a crashed editor.
        self.segments_ready = True
        self.segments_error: str | None = None
        # Whether `initial_segments` came from a saved project file rather
        # than fresh silence detection — see #14. Once resumed, the
        # background scan below still fills in `initial_silences` (for the
        # threshold sliders) but must not overwrite these with freshly
        # detected segments.
        self.resumed = False


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
    session = _Session()

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

    def _require_open() -> Path:
        if session.source is None:
            raise HTTPException(status_code=409, detail="no file is open; call /api/open first")
        return session.source

    def _detect_initial_segments(cache: SilenceCache, total_duration: float) -> None:
        """Runs on a background thread — see `_open`. Only ever touches
        `session` under `session.lock`, since it races the request thread(s)
        reading `_state_response()` while this is still running."""
        try:
            silences = cache.get_raw_silences(defaults.silence_threshold)
            segments = compute_segments(
                silences,
                total_duration,
                defaults.min_silence_duration,
                defaults.min_song_length,
                defaults.padding,
            )
        except (RuntimeError, OSError) as exc:
            with session.lock:
                session.segments_error = str(exc)
                session.segments_ready = True
            return
        with session.lock:
            session.initial_silences = silences
            if not session.resumed:
                session.initial_segments = _to_saved(segments)
            session.segments_ready = True

    def _open(source: Path) -> None:
        session.source = source
        session.total_duration = probe_duration(source)
        session.cache = SilenceCache(source)
        session.waveform_cache = WaveformCache(source)
        session.classification_cache = ClassificationCache(source)
        session.initial_silences = []
        session.segments_error = None

        saved = load_project(source)
        if saved is not None:
            session.initial_segments = saved
            session.resumed = True
            session.segments_ready = True  # already have splits to show — no need to wait on detection
        else:
            session.initial_segments = []
            session.resumed = False
            session.segments_ready = False

        thread = threading.Thread(
            target=_detect_initial_segments,
            args=(session.cache, session.total_duration),
            daemon=True,
        )
        thread.start()

    def _state_response() -> StateResponse:
        source = _require_open()
        with session.lock:
            return StateResponse(
                filename=source.name,
                duration=session.total_duration,
                video_url="/media/source",
                params=defaults,
                silences=_silences_out(session.initial_silences),
                segments=_segments_out(session.initial_segments),
                segments_ready=session.segments_ready,
                segments_error=session.segments_error,
                resumed=session.resumed,
            )

    if root.is_file():
        _open(root)

    @app.get("/api/session", response_model=SessionResponse)
    def get_session() -> SessionResponse:
        return SessionResponse(
            file_open=session.source is not None,
            filename=session.source.name if session.source is not None else None,
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
        try:
            resolved = resolve_within_root(browse_root, request.path)
        except PathEscapesRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root")
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        _open(resolved)
        return _state_response()

    @app.get("/api/state", response_model=StateResponse)
    def get_state() -> StateResponse:
        return _state_response()

    @app.post("/api/project", response_model=StateResponse)
    def save_current_project(request: ProjectSaveRequest) -> StateResponse:
        source = _require_open()
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
        save_project(source, segments)
        with session.lock:
            session.initial_segments = segments
            session.resumed = True
        return _state_response()

    @app.get("/media/source")
    def get_media() -> FileResponse:
        return FileResponse(_require_open())

    @app.post("/api/detect", response_model=DetectResponse)
    def detect(request: DetectRequest) -> DetectResponse:
        _require_open()
        silences = session.cache.get_raw_silences(request.silence_threshold)
        return DetectResponse(silences=_silences_out(silences))

    @app.get("/api/waveform", response_model=WaveformResponse)
    def waveform() -> WaveformResponse:
        _require_open()
        peaks = session.waveform_cache.get_peaks()
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
    def analyze() -> AnalyzeStartResponse:
        source = _require_open()
        cache = session.classification_cache
        if cache.is_analyzed:
            # Already have a result — this session, or loaded from a
            # sidecar cache on disk. Report a job that's already "done"
            # instead of re-decoding audio and rerunning inference.
            job_id, _ = analysis_job_store.create_done(total=0)
            return AnalyzeStartResponse(job_id=job_id)
        thresholds = cache.get_thresholds()
        job_id = start_audio_analysis(analysis_job_store, source, cache, thresholds)
        return AnalyzeStartResponse(job_id=job_id)

    @app.get("/api/analyze/{job_id}", response_model=AnalyzeStatusResponse)
    def analyze_status(job_id: str) -> AnalyzeStatusResponse:
        job = analysis_job_store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job id")
        with job.lock:
            return AnalyzeStatusResponse(status=job.status, completed=job.completed, total=job.total, error=job.error)

    @app.get("/api/classification", response_model=ClassificationResponse)
    def classification() -> ClassificationResponse:
        _require_open()
        cache = session.classification_cache
        return _classification_out(cache, cache.is_analyzed)

    @app.post("/api/classification/thresholds", response_model=ClassificationResponse)
    def rethreshold_classification(request: RethresholdRequest) -> ClassificationResponse:
        _require_open()
        cache = session.classification_cache
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
    def export(request: ExportRequest) -> ExportStartResponse:
        source = _require_open()
        pairs = [(s.start, s.end) for s in request.segments]
        _validate_segments(pairs)

        output_dir = source.with_name(f"{source.stem}_split")
        ext = source.suffix if request.output_format is None else f".{request.output_format.lstrip('.')}"
        filenames = [
            resolve_export_filename(i + 1, len(pairs), source.stem, ext, s.name) for i, s in enumerate(request.segments)
        ]
        if len(set(filenames)) != len(filenames):
            raise HTTPException(status_code=422, detail="export names must be unique across segments")

        if not request.overwrite:
            conflicts = [f for f in filenames if (output_dir / f).exists()]
            if conflicts:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{len(conflicts)} output file(s) already exist "
                        f"(pass overwrite=true to replace): {', '.join(conflicts)}"
                    ),
                )

        job_id = start_export(
            job_store,
            source,
            pairs,
            filenames,
            output_dir,
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

    # Mounted last: Starlette matches routes in registration order, and this
    # catch-all static mount must not shadow the /api/* and /media/* routes.
    # The static/ directory is owned by the frontend build; if it's not
    # populated yet, drop a placeholder so the mount doesn't error.
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    if not any(STATIC_DIR.iterdir()):
        (STATIC_DIR / "index.html").write_text("<!doctype html><title>split-video editor</title><p>loading...</p>")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


def _validate_segments(pairs: list[tuple[float, float]]) -> None:
    if not pairs:
        raise HTTPException(status_code=422, detail="segments list must not be empty")
    if sorted(pairs, key=lambda p: p[0]) != pairs:
        raise HTTPException(status_code=422, detail="segments must be sorted by start time")
    for start, end in pairs:
        if end <= start:
            raise HTTPException(status_code=422, detail=f"segment end must be after start: ({start}, {end})")
    for (_, end), (next_start, _) in itertools.pairwise(pairs):
        if next_start < end:
            raise HTTPException(status_code=422, detail="segments must not overlap")
