"""In-memory job state (export, audio analysis), run in a background thread.

Scoped to one `split-video edit` server process — no disk/multi-process
persistence, which is the right amount of machinery for a single-user local
tool that lives exactly as long as the process does.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from split_video.editor import classify
from split_video.editor.cache import ClassificationCache
from split_video.ffmpeg import (
    ExtractError,
    ProbeError,
    extract_pcm_audio,
    extract_segment,
    probe_duration,
)
from split_video.naming import build_manifest, segment_filename, write_manifest
from split_video.segments import Segment


class ExportJob:
    def __init__(self, total: int) -> None:
        self.lock = threading.Lock()
        self.status = "running"
        self.completed = 0
        self.total = total
        self.current_file: str | None = None
        self.output_dir: str | None = None
        self.manifest_path: str | None = None
        self.files: list[str] = []
        self.error: str | None = None


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, ExportJob] = {}
        self._lock = threading.Lock()

    def create(self, total: int) -> tuple[str, ExportJob]:
        job_id = uuid.uuid4().hex
        job = ExportJob(total)
        with self._lock:
            self._jobs[job_id] = job
        return job_id, job

    def get(self, job_id: str) -> ExportJob | None:
        with self._lock:
            return self._jobs.get(job_id)


def start_export(
    job_store: JobStore,
    source: Path,
    segments: list[tuple[float, float]],
    output_dir: Path,
    precise: bool,
    output_format: str | None,
    write_manifest_file: bool,
    parameters: dict[str, Any],
) -> str:
    job_id, job = job_store.create(len(segments))
    thread = threading.Thread(
        target=_run_export,
        args=(job, source, segments, output_dir, precise, output_format, write_manifest_file, parameters),
        daemon=True,
    )
    thread.start()
    return job_id


def _run_export(
    job: ExportJob,
    source: Path,
    segments: list[tuple[float, float]],
    output_dir: Path,
    precise: bool,
    output_format: str | None,
    write_manifest_file: bool,
    parameters: dict[str, Any],
) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        ext = source.suffix if output_format is None else f".{output_format.lstrip('.')}"
        filenames = [segment_filename(i + 1, len(segments), source.stem, ext) for i in range(len(segments))]
        segment_objs = [Segment(index=i + 1, start=start, end=end) for i, (start, end) in enumerate(segments)]

        for segment, filename in zip(segment_objs, filenames):
            out_path = output_dir / filename
            with job.lock:
                job.current_file = filename
            extract_segment(source, segment.start, segment.end, out_path, precise, output_format)
            with job.lock:
                job.completed += 1
                job.files.append(str(out_path))

        with job.lock:
            job.output_dir = str(output_dir)

        if write_manifest_file:
            manifest_data = build_manifest(
                source_path=source,
                segments=segment_objs,
                filenames=filenames,
                parameters=parameters,
                generated_at=datetime.now(timezone.utc),
            )
            manifest_path = write_manifest(manifest_data, output_dir)
            with job.lock:
                job.manifest_path = str(manifest_path)

        with job.lock:
            job.status = "done"
    except ExtractError as exc:
        with job.lock:
            job.status = "error"
            job.error = str(exc)
    except OSError as exc:
        with job.lock:
            job.status = "error"
            job.error = str(exc)


class AnalysisJob:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status = "running"
        self.completed = 0
        self.total = 0
        self.error: str | None = None


class AnalysisJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, AnalysisJob] = {}
        self._lock = threading.Lock()

    def create(self) -> tuple[str, AnalysisJob]:
        job_id = uuid.uuid4().hex
        job = AnalysisJob()
        with self._lock:
            self._jobs[job_id] = job
        return job_id, job

    def get(self, job_id: str) -> AnalysisJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def create_done(self, total: int) -> tuple[str, AnalysisJob]:
        """A job that reports as already finished — for when the caller
        (see `analyze()` in app.py) has nothing new to run, e.g. the
        classification cache was already populated (this session, or
        loaded from a sidecar cache on disk) before "Analyze audio" was
        clicked. Keeps the frontend's poll-until-done flow unchanged."""
        job_id, job = self.create()
        with job.lock:
            job.status = "done"
            job.completed = total
            job.total = total
        return job_id, job


def start_audio_analysis(
    job_store: AnalysisJobStore,
    source: Path,
    cache: ClassificationCache,
    thresholds: dict[str, float],
) -> str:
    job_id, job = job_store.create()
    thread = threading.Thread(target=_run_analysis, args=(job, source, cache, thresholds), daemon=True)
    thread.start()
    return job_id


def _run_analysis(job: AnalysisJob, source: Path, cache: ClassificationCache, thresholds: dict[str, float]) -> None:
    try:
        # Set an upfront estimate from duration alone so the progress bar
        # isn't stuck at 0/0 during the (possibly slow) PCM decode below,
        # which happens before the real per-frame count is known.
        with job.lock:
            job.total = max(1, int(probe_duration(source) * classify.SAMPLE_RATE) // classify.HOP_SAMPLES)

        model, labels = classify.get_model_and_labels()
        pcm = extract_pcm_audio(source, sample_rate=classify.SAMPLE_RATE)

        def on_progress(completed: int, total: int) -> None:
            with job.lock:
                job.completed = completed
                job.total = total

        regions, frame_bucket_scores, total_duration = classify.classify_audio(
            pcm, model, labels, thresholds, on_progress
        )
        cache.store_result(frame_bucket_scores, total_duration, regions)
        with job.lock:
            job.status = "done"
    except (classify.ModelNotFoundError, ExtractError, ProbeError, OSError) as exc:
        with job.lock:
            job.status = "error"
            job.error = str(exc)
