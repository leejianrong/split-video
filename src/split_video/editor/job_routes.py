"""HTTP routes over the job queue: any job by id, whatever its kind, and cancelling it.

The export, analysis and render endpoints keep their own start and status routes (and response
shapes); these are the generic view of the same jobs.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from split_video.editor.schemas import JobListOut, JobOut
from split_video.jobs import JobInfo, JobQueue, UnknownJobError

# How long a cancel request waits for the job to actually stop before answering with its
# current (still running, `cancel_requested`) snapshot. ffmpeg is killed after 2 seconds at the latest.
CANCEL_WAIT_SECONDS = 4.0


def job_out(info: JobInfo) -> JobOut:
    return JobOut(
        id=info.id,
        kind=info.kind,
        state=info.state,
        label=info.label,
        completed=info.completed,
        total=info.total,
        fraction=info.fraction,
        message=info.message,
        result=info.result,
        error=info.error,
        created_at=info.created_at,
        started_at=info.started_at,
        finished_at=info.finished_at,
        cancel_requested=info.cancel_requested,
    )


def job_of_kind(jobs: JobQueue, job_id: str, kind: str | None = None) -> JobInfo:
    """The job's snapshot, 404 if the id is unknown or (when `kind` is given) belongs to another kind of job."""
    try:
        info = jobs.status(job_id)
    except UnknownJobError:
        raise HTTPException(status_code=404, detail="unknown job id") from None
    if kind is not None and info.kind != kind:
        raise HTTPException(status_code=404, detail="unknown job id")
    return info


def cancel_job(jobs: JobQueue, job_id: str, kind: str | None = None) -> JobInfo:
    """Cancel and return the job's snapshot, waiting briefly for a running job to stop."""
    job_of_kind(jobs, job_id, kind)
    try:
        info = jobs.cancel(job_id)
        if not info.finished:
            info = jobs.wait(job_id, CANCEL_WAIT_SECONDS)
    except UnknownJobError:
        raise HTTPException(status_code=404, detail="unknown job id") from None
    return info


def make_router(jobs: JobQueue) -> APIRouter:
    router = APIRouter()

    @router.get("/jobs", response_model=JobListOut)
    def list_jobs(kind: str | None = None, state: str | None = None) -> JobListOut:
        """Every job this server remembers, newest first, optionally of one `kind` and/or `state`."""
        return JobListOut(jobs=[job_out(i) for i in jobs.list(kind=kind, state=state)])

    @router.get("/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: str) -> JobOut:
        """One job by id, still available after it finishes (until the server restarts)."""
        return job_out(job_of_kind(jobs, job_id))

    @router.post("/jobs/{job_id}/cancel", response_model=JobOut)
    def cancel(job_id: str) -> JobOut:
        """Ask a job to stop. Idempotent; a finished job is returned unchanged. A running job is
        terminated (ffmpeg included) and the answer waits a few seconds for it to report `cancelled`."""
        return job_out(cancel_job(jobs, job_id))

    return router
