"""The render queue: several renders from one instruction, run in the background with limited concurrency.

A batch is a list of render requests, each one what a single render accepts (`timeline_service.plan_render` plus
the caption options) and each one a `render` job on the ordinary job queue (`jobs.py`), tagged with a shared
`batch_id` (the job's `group`). The queue caps how many renders run at once (`jobs.render_concurrency_from_env`,
default 2): the rest wait as `queued`, and a queued render that is cancelled never starts. Renders are independent:
one that fails (a bad request, a missing asset, an ffmpeg error) ends as `failed` with a structured error and
its siblings carry on. A failed or cancelled render leaves no partial output file (`render.render`).

The HTTP routes, the CLI and the MCP tools all call the functions here and report the same JSON: `submit_batch`
and `batch_status` return `{batch_id, concurrency, total, counts, finished, jobs: [...]}` with one entry per
request, in request order. A job's `state` is queued, running, done, failed or cancelled.
"""

from __future__ import annotations

import uuid
from typing import Any

from split_video import captions, timeline_service
from split_video.error_catalogue import describe
from split_video.identity import LOCAL_PRINCIPAL, Principal, can_see_job
from split_video.job_work import RENDER_STEPS, render_work
from split_video.jobs import RENDER, JobInfo, JobQueue, UnknownJobError
from split_video.media_store import MediaStore
from split_video.store import ProjectStore
from split_video.timeline import TimelineError

MAX_BATCH = 50  # requests in one batch
STATES = ("queued", "running", "done", "failed", "cancelled")
# How long cancelling waits for running renders to stop (ffmpeg is killed after 2 seconds at the latest).
CANCEL_WAIT_SECONDS = 4.0

# The keys of one render request.
REQUEST_KEYS = frozenset(
    {
        "project",
        "output",
        "overwrite",
        "width",
        "height",
        "preset",
        "fit",
        "captions",
        "caption_range",
        "caption_font_size",
        "caption_highlight_color",
        "caption_position",
        "caption_tracks",
    }
)


class RenderQueueError(ValueError):
    """The batch itself is unusable (empty, too long, not a list)."""


class UnknownBatchError(RenderQueueError):
    """No render batch has this id (never submitted, evicted, or another principal's)."""


class RenderRequestError(TimelineError):
    """One request of a batch is malformed (not an object, no project, an unknown key, a duplicate output)."""


def _wrong_type(key: str, wanted: str) -> RenderRequestError:
    return RenderRequestError(f"{key} must be {wanted}")


def _check_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RenderRequestError("each render request is an object with at least a project name")
    unknown = sorted(set(raw) - REQUEST_KEYS)
    if unknown:
        raise RenderRequestError(
            f"unknown render option(s): {', '.join(unknown)}; valid: {', '.join(sorted(REQUEST_KEYS))}"
        )
    if not isinstance(raw.get("project"), str) or not raw["project"]:
        raise RenderRequestError("each render request needs a project (the timeline's name)")
    if raw.get("output") is not None and not isinstance(raw["output"], str):
        raise _wrong_type("output", "a filename")
    if not isinstance(raw.get("overwrite", False), bool):
        raise _wrong_type("overwrite", "true or false")
    return raw


def _label(raw: Any) -> str | None:
    """The output a request will write, for a request that fails before it is planned."""
    if not isinstance(raw, dict) or not isinstance(raw.get("project"), str):
        return None
    output = raw.get("output")
    return output if isinstance(output, str) else f"{raw['project']}-render.mp4"


def submit_batch(
    jobs: JobQueue,
    store: ProjectStore,
    media: MediaStore,
    requests: Any,
    *,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Queue one render per request and return `batch_status` for the batch, in request order.

    Every request is checked up front (`plan_render`: project, output name, preset, size, captions, existing
    output). A request that fails the check does not stop the batch: it gets a job that is already `failed`
    with the reason, so the answer always has one job id per request. Two requests of one batch may not write
    the same output file (the second fails). Raises `RenderQueueError` if there are no requests or too many."""
    if not isinstance(requests, list | tuple):
        raise RenderQueueError("renders must be a list of render requests")
    if not requests:
        raise RenderQueueError("renders is empty: give at least one render request")
    if len(requests) > MAX_BATCH:
        raise RenderQueueError(f"at most {MAX_BATCH} renders per batch, got {len(requests)}")
    batch_id = uuid.uuid4().hex
    owner = None if principal is None else principal.id
    claimed: set[str] = set()
    for raw in requests:
        label = _label(raw)
        try:
            spec = _check_request(raw)
            if label in claimed:
                raise RenderRequestError(f"{label} is already the output of another render in this batch")
            caption_request = captions.request(
                spec.get("captions"),
                spec.get("caption_range"),
                spec.get("caption_font_size"),
                spec.get("caption_highlight_color"),
                spec.get("caption_position"),
                spec.get("caption_tracks"),
            )
            project, filename, out_id = timeline_service.plan_render(
                store,
                media,
                spec["project"],
                spec.get("output"),
                spec.get("overwrite", False),
                spec.get("width"),
                spec.get("height"),
                spec.get("preset"),
                spec.get("fit"),
                caption_request,
            )
            claimed.add(out_id)
        except (TimelineError, ValueError) as exc:
            jobs.record_failed(
                RENDER,
                error=str(exc) or type(exc).__name__,
                error_code=type(exc).__name__,
                error_info=_info_of(exc),
                label=label,
                total=RENDER_STEPS,
                owner=owner,
                group=batch_id,
            )
            continue
        work = render_work(
            project,
            media,
            out_id,
            filename,
            spec.get("width"),
            spec.get("height"),
            spec.get("preset"),
            spec.get("fit"),
            spec["project"],
            caption_request,
        )
        jobs.submit(RENDER, work, label=filename, total=RENDER_STEPS, owner=owner, group=batch_id)
    return batch_status(jobs, batch_id, LOCAL_PRINCIPAL if principal is None else principal)


def _entry(index: int, info: JobInfo) -> dict[str, Any]:
    return {
        "index": index,
        "job_id": info.id,
        "output": info.label,
        "state": "failed" if info.state == "error" else info.state,
        "progress": round(info.fraction, 4),
        "error": _error_of(info),
        "result": info.result if info.state == "done" else None,
        "started_at": info.started_at,
        "finished_at": info.finished_at,
    }


def _info_of(exc: Exception) -> dict[str, str]:
    described = describe(exc)
    return {"code": described.code, "message": str(exc) or type(exc).__name__, "fix": described.fix}


def _error_of(info: JobInfo) -> dict[str, str] | None:
    """The failure as `{code, message, fix}`: the error catalogue's code (docs/errors.md), what happened, what to do."""
    if info.state != "error":
        return None
    if info.error_info is not None:
        return info.error_info
    return {"code": "error.internal", "message": info.error or "render failed", "fix": describe(RuntimeError()).fix}


def _describe(jobs: JobQueue, batch_id: str | None, infos: list[JobInfo]) -> dict[str, Any]:
    entries = [_entry(n + 1, i) for n, i in enumerate(infos)]
    counts = {state: sum(1 for e in entries if e["state"] == state) for state in STATES}
    return {
        "batch_id": batch_id,
        "concurrency": jobs.concurrency(RENDER),
        "total": len(entries),
        "counts": counts,
        "finished": counts["queued"] == 0 and counts["running"] == 0,
        "jobs": entries,
    }


def batch_status(jobs: JobQueue, batch_id: str, viewer: Principal = LOCAL_PRINCIPAL) -> dict[str, Any]:
    """The batch's renders, in request order, with state, progress, error and result. `UnknownBatchError` if
    no render of this batch is known to `viewer`."""
    # `list` is newest first and stable per submission, so reversing gives request order even when timestamps tie.
    infos = [i for i in reversed(jobs.list(kind=RENDER, group=batch_id)) if can_see_job(i.owner, viewer)]
    if not infos:
        raise UnknownBatchError(f"unknown render batch {batch_id!r}")
    return _describe(jobs, batch_id, infos)


def queue_status(jobs: JobQueue, viewer: Principal = LOCAL_PRINCIPAL) -> dict[str, Any]:
    """Every render the queue knows (all batches and single renders), in the order they were queued."""
    infos = [i for i in reversed(jobs.list(kind=RENDER)) if can_see_job(i.owner, viewer)]
    return _describe(jobs, None, infos)


def cancel_renders(
    jobs: JobQueue,
    *,
    batch_id: str | None = None,
    job_id: str | None = None,
    viewer: Principal = LOCAL_PRINCIPAL,
) -> dict[str, Any]:
    """Cancel one render (`job_id`) or every unfinished render of a batch (`batch_id`) and return the batch's
    status (the queue's, for a lone `job_id` without a batch). Queued renders are cancelled at once and never
    start; running ones have ffmpeg terminated and their partial output deleted. Idempotent; finished renders are
    left as they are. `UnknownBatchError` / `UnknownJobError` for an id that is not known to `viewer`."""
    if (batch_id is None) == (job_id is None):
        raise RenderQueueError("give exactly one of batch_id and job_id")
    if job_id is not None:
        try:
            info = jobs.status(job_id)
        except UnknownJobError:
            raise UnknownJobError(f"unknown render job {job_id!r}") from None
        if info.kind != RENDER or not can_see_job(info.owner, viewer):
            raise UnknownJobError(f"unknown render job {job_id!r}")
        targets = [info]
    else:
        targets = [i for i in jobs.list(kind=RENDER, group=batch_id) if can_see_job(i.owner, viewer)]
        if not targets:
            raise UnknownBatchError(f"unknown render batch {batch_id!r}")
    for info in targets:
        if not info.finished:
            jobs.cancel(info.id)
    for info in targets:
        jobs.wait(info.id, CANCEL_WAIT_SECONDS)
    group = targets[0].group if job_id is not None else batch_id
    if group is not None:
        return batch_status(jobs, group, viewer)
    return _describe(jobs, None, [jobs.status(i.id) for i in targets])
