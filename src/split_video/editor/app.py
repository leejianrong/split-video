"""FastAPI app for the browser-based split-point editor."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from split_video.editor.cache import SilenceCache
from split_video.editor.jobs import JobStore, start_export
from split_video.editor.schemas import (
    DetectRequest,
    DetectResponse,
    ExportRequest,
    ExportStartResponse,
    ExportStatusResponse,
    SegmentOut,
    SegmentsRequest,
    SegmentsResponse,
    SilenceIntervalOut,
    StateParams,
    StateResponse,
)
from split_video.ffmpeg import probe_duration
from split_video.naming import segment_filename
from split_video.segments import Segment, compute_segments
from split_video.silence import SilenceInterval

STATIC_DIR = Path(__file__).parent / "static"


def create_app(source: Path, defaults: StateParams) -> FastAPI:
    app = FastAPI(title="split-video editor")

    total_duration = probe_duration(source)
    cache = SilenceCache(source)
    job_store = JobStore()

    initial_silences = cache.get_raw_silences(defaults.silence_threshold)
    initial_segments = compute_segments(
        initial_silences,
        total_duration,
        defaults.min_silence_duration,
        defaults.min_song_length,
        defaults.padding,
    )

    def _segments_out(segments: list[Segment]) -> list[SegmentOut]:
        return [SegmentOut(index=s.index, start=s.start, end=s.end, duration=s.duration) for s in segments]

    def _silences_out(silences: list[SilenceInterval]) -> list[SilenceIntervalOut]:
        return [SilenceIntervalOut(start=s.start, end=s.end) for s in silences]

    @app.get("/api/state", response_model=StateResponse)
    def get_state() -> StateResponse:
        return StateResponse(
            filename=source.name,
            duration=total_duration,
            video_url="/media/source",
            params=defaults,
            silences=_silences_out(initial_silences),
            segments=_segments_out(initial_segments),
        )

    @app.get("/media/source")
    def get_media() -> FileResponse:
        return FileResponse(source)

    @app.post("/api/detect", response_model=DetectResponse)
    def detect(request: DetectRequest) -> DetectResponse:
        silences = cache.get_raw_silences(request.silence_threshold)
        return DetectResponse(silences=_silences_out(silences))

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
    for (_, end), (next_start, _) in zip(pairs, pairs[1:]):
        if next_start < end:
            raise HTTPException(status_code=422, detail="segments must not overlap")
