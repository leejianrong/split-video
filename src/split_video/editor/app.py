"""FastAPI app for the browser-based split-point editor."""

from __future__ import annotations

import itertools
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from split_video.editor.browse import (
    PathEscapesRootError,
    list_directory,
    resolve_within_root,
)
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.editor.jobs import (
    AnalysisJobStore,
    JobStore,
    start_audio_analysis,
    start_export,
)
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
from split_video.naming import segment_filename
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
        self.source: Path | None = None
        self.total_duration: float = 0.0
        self.cache: SilenceCache | None = None
        self.waveform_cache: WaveformCache | None = None
        self.classification_cache: ClassificationCache | None = None
        self.initial_silences: list[SilenceInterval] = []
        self.initial_segments: list[Segment] = []


def create_app(root: Path, defaults: StateParams) -> FastAPI:
    """`root` is either a video file to open immediately (the historical
    `split-video edit <file>` behavior, unchanged) or a directory to pick a
    video from via the in-browser file picker."""
    app = FastAPI(title="split-video editor")

    browse_root = root.parent if root.is_file() else root
    job_store = JobStore()
    analysis_job_store = AnalysisJobStore()
    session = _Session()

    def _segments_out(segments: list[Segment]) -> list[SegmentOut]:
        return [SegmentOut(index=s.index, start=s.start, end=s.end, duration=s.duration) for s in segments]

    def _silences_out(silences: list[SilenceInterval]) -> list[SilenceIntervalOut]:
        return [SilenceIntervalOut(start=s.start, end=s.end) for s in silences]

    def _require_open() -> Path:
        if session.source is None:
            raise HTTPException(status_code=409, detail="no file is open; call /api/open first")
        return session.source

    def _open(source: Path) -> None:
        session.source = source
        session.total_duration = probe_duration(source)
        session.cache = SilenceCache(source)
        session.waveform_cache = WaveformCache(source)
        session.classification_cache = ClassificationCache()
        session.initial_silences = session.cache.get_raw_silences(defaults.silence_threshold)
        session.initial_segments = compute_segments(
            session.initial_silences,
            session.total_duration,
            defaults.min_silence_duration,
            defaults.min_song_length,
            defaults.padding,
        )

    def _state_response() -> StateResponse:
        source = _require_open()
        return StateResponse(
            filename=source.name,
            duration=session.total_duration,
            video_url="/media/source",
            params=defaults,
            silences=_silences_out(session.initial_silences),
            segments=_segments_out(session.initial_segments),
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

    def _classification_out(cache: ClassificationCache, analyzed: bool) -> ClassificationResponse:
        return ClassificationResponse(
            analyzed=analyzed,
            regions=[
                ClassificationRegionOut(start=r.start, end=r.end, bucket=r.bucket, score=r.score)
                for r in cache.get_regions()
            ],
            thresholds=cache.get_thresholds(),
        )

    @app.post("/api/analyze", response_model=AnalyzeStartResponse)
    def analyze() -> AnalyzeStartResponse:
        source = _require_open()
        thresholds = session.classification_cache.get_thresholds()
        job_id = start_audio_analysis(analysis_job_store, source, session.classification_cache, thresholds)
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
        return SegmentsResponse(segments=_segments_out(computed))

    @app.post("/api/export", response_model=ExportStartResponse)
    def export(request: ExportRequest) -> ExportStartResponse:
        source = _require_open()
        pairs = [(s.start, s.end) for s in request.segments]
        _validate_segments(pairs)

        output_dir = source.with_name(f"{source.stem}_split")
        ext = source.suffix if request.output_format is None else f".{request.output_format.lstrip('.')}"
        filenames = [segment_filename(i + 1, len(pairs), source.stem, ext) for i in range(len(pairs))]

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
        (STATIC_DIR / "index.html").write_text(
            "<!doctype html><title>split-video editor</title><p>loading...</p>"
        )
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
