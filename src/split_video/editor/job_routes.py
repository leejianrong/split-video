"""HTTP routes over the job queue: any job by id, whatever its kind, and cancelling it.

The export, analysis and render endpoints keep their own start and status routes (and response
shapes); these are the generic view of the same jobs.
"""

from __future__ import annotations

from fastapi import APIRouter

from split_video.editor.auth import principal_dependency
from split_video.editor.errors import api_error
from split_video.editor.schemas import JobListOut, JobOut
from split_video.identity import LOCAL_PRINCIPAL, Principal, can_see_job
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
        error_code=info.error_code,
        error_info=info.error_info,
        created_at=info.created_at,
        started_at=info.started_at,
        finished_at=info.finished_at,
        cancel_requested=info.cancel_requested,
        owner=info.owner,
        group=info.group,
    )


def job_of_kind(
    jobs: JobQueue, job_id: str, kind: str | None = None, principal: Principal = LOCAL_PRINCIPAL
) -> JobInfo:
    """The job's snapshot, 404 if the id is unknown, belongs to another kind of job (when `kind` is given),
    or is another principal's (which looks exactly like an unknown id)."""
    try:
        info = jobs.status(job_id)
    except UnknownJobError:
        raise api_error(404, UnknownJobError("unknown job id")) from None
    if (kind is not None and info.kind != kind) or not can_see_job(info.owner, principal):
        raise api_error(404, UnknownJobError("unknown job id"))
    return info


def cancel_job(jobs: JobQueue, job_id: str, kind: str | None = None, principal: Principal = LOCAL_PRINCIPAL) -> JobInfo:
    """Cancel and return the job's snapshot, waiting briefly for a running job to stop."""
    job_of_kind(jobs, job_id, kind, principal)
    try:
        info = jobs.cancel(job_id)
        if not info.finished:
            info = jobs.wait(job_id, CANCEL_WAIT_SECONDS)
    except UnknownJobError:
        raise api_error(404, UnknownJobError("unknown job id")) from None
    return info


def make_router(jobs: JobQueue) -> APIRouter:
    router = APIRouter()

    @router.get("/jobs", response_model=JobListOut)
    def list_jobs(
        kind: str | None = None, state: str | None = None, principal: Principal = principal_dependency
    ) -> JobListOut:
        """The caller's jobs, newest first, optionally of one `kind` and/or `state`."""
        return JobListOut(
            jobs=[job_out(i) for i in jobs.list(kind=kind, state=state) if can_see_job(i.owner, principal)]
        )

    @router.get("/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: str, principal: Principal = principal_dependency) -> JobOut:
        """One job by id, still available after it finishes (until the server restarts)."""
        return job_out(job_of_kind(jobs, job_id, None, principal))

    @router.post("/jobs/{job_id}/cancel", response_model=JobOut)
    def cancel(job_id: str, principal: Principal = principal_dependency) -> JobOut:
        """Ask a job to stop. Idempotent; a finished job is returned unchanged. A running job is
        terminated (ffmpeg included) and the answer waits a few seconds for it to report `cancelled`."""
        return job_out(cancel_job(jobs, job_id, None, principal))

    return router
