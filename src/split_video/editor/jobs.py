"""In-memory job state (export, audio analysis), run in a background thread.

Scoped to one `split-video edit` server process — no disk/multi-process
persistence, which is the right amount of machinery for a single-user local
tool that lives exactly as long as the process does.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from split_video import project
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache
from split_video.ffmpeg import ExtractError, ProbeError, probe_duration


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
    plan: project.ExportPlan,
    precise: bool,
    output_format: str | None,
    write_manifest_file: bool,
    parameters: dict[str, Any],
) -> str:
    job_id, job = job_store.create(len(plan.segments))
    thread = threading.Thread(
        target=_run_export,
        args=(job, plan, precise, output_format, write_manifest_file, parameters),
        daemon=True,
    )
    thread.start()
    return job_id


def _run_export(
    job: ExportJob,
    plan: project.ExportPlan,
    precise: bool,
    output_format: str | None,
    write_manifest_file: bool,
    parameters: dict[str, Any],
) -> None:
    def on_file_start(filename: str) -> None:
        with job.lock:
            job.current_file = filename

    def on_file_done(path: str) -> None:
        with job.lock:
            job.completed += 1
            job.files.append(path)

    try:
        _, manifest_path = project.run_export(
            plan,
            precise=precise,
            output_format=output_format,
            write_manifest_file=write_manifest_file,
            parameters=parameters,
            on_file_start=on_file_start,
            on_file_done=on_file_done,
        )
        with job.lock:
            job.output_dir = str(plan.output_dir)
            if manifest_path is not None:
                job.manifest_path = str(manifest_path)
            job.status = "done"
    except (ExtractError, OSError) as exc:
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

        def on_progress(completed: int, total: int) -> None:
            with job.lock:
                job.completed = completed
                job.total = total

        project.run_analysis(source, cache, thresholds, on_progress)
        with job.lock:
            job.status = "done"
    except (classify.ModelNotFoundError, ExtractError, ProbeError, OSError) as exc:
        with job.lock:
            job.status = "error"
            job.error = str(exc)
