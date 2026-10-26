"""The job queue seam: submit long work, poll or subscribe to it, cancel it, fetch its result.

Export, audio analysis, timeline renders, proxies, scans, transcription and scene analysis are all "a function
that takes a while". They run through one `JobQueue` interface instead of ad hoc threads, so
the editor's HTTP API, a future event stream (SPL-37) and a hosted worker pool (SPL-38 added
who owns a job) all talk to the same thing. `InProcessJobQueue` is the local implementation:
a bounded pool of daemon threads inside the server process. It is not a distributed queue.

Job kinds are plain strings: `export`, `analysis`, `render`, `library_scan`, `proxy`, `transcription`, `scene_analysis`, `ocr`, `split` and `waveform`.

Cancellation is cooperative. `cancel` flags the job and runs the callbacks its work registered
with `JobContext.on_cancel` (that is how a running ffmpeg is terminated at once); the work
then raises `JobCancelledError`, usually through `ctx.raise_if_cancelled()` or because a
helper such as `ffmpeg.run_cancellable` saw the flag. A job still waiting in the queue is
cancelled immediately and never starts. Work that never checks simply finishes as `done`.

This module imports nothing from the rest of the engine, so any engine module may take a
`CancelToken` (which `JobContext` satisfies) without a cycle.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, runtime_checkable

from split_video.error_catalogue import describe

log = logging.getLogger(__name__)

# Job kinds.
EXPORT = "export"
ANALYSIS = "analysis"
RENDER = "render"
LIBRARY_SCAN = "library_scan"
PROXY = "proxy"
TRANSCRIPTION = "transcription"
SCENE_ANALYSIS = "scene_analysis"
OCR = "ocr"
SPLIT = "split"
WAVEFORM = "waveform"

JobState = Literal["queued", "running", "done", "error", "cancelled"]
FINISHED_STATES: frozenset[str] = frozenset({"done", "error", "cancelled"})

DEFAULT_MAX_WORKERS = 2
# How many renders may run at once. ffmpeg renders are memory-heavy, so the default is low and the bound is hard.
DEFAULT_RENDER_CONCURRENCY = 2
MAX_RENDER_CONCURRENCY = 8
RENDER_CONCURRENCY_ENV = "SPLIT_VIDEO_RENDER_CONCURRENCY"
# Finished jobs are kept in memory so `status`/`result` work by id after completion. Past this many,
# the oldest finished job is forgotten (running and queued jobs are never evicted).
DEFAULT_MAX_RETAINED = 1000


def check_render_concurrency(value: int) -> int:
    """`value` if it is a usable number of simultaneous renders (1..MAX_RENDER_CONCURRENCY), else ValueError."""
    if not 1 <= value <= MAX_RENDER_CONCURRENCY:
        raise ValueError(f"render concurrency must be between 1 and {MAX_RENDER_CONCURRENCY}, got {value}")
    return value


def render_concurrency_from_env() -> int:
    """The configured render concurrency: `SPLIT_VIDEO_RENDER_CONCURRENCY`, else the default (2)."""
    raw = os.environ.get(RENDER_CONCURRENCY_ENV, "").strip()
    if not raw:
        return DEFAULT_RENDER_CONCURRENCY
    try:
        return check_render_concurrency(int(raw))
    except ValueError:
        raise ValueError(
            f"{RENDER_CONCURRENCY_ENV} must be a whole number between 1 and {MAX_RENDER_CONCURRENCY}, got {raw!r}"
        ) from None


class JobError(Exception):
    """Base class for job-queue failures."""


class UnknownJobError(JobError):
    """No job has this id (never submitted, or a finished one was evicted)."""


class JobNotFinishedError(JobError):
    """`result` was asked for a job that is still queued or running."""


class JobFailedError(JobError):
    """`result` was asked for a job whose work raised; the message is the work's error."""


class JobCancelledError(JobError):
    """Raised by work that noticed its job was cancelled; `result` raises it for a cancelled job."""


@dataclass(frozen=True)
class JobInfo:
    """An immutable snapshot of one job.

    `completed`/`total` are in whatever unit the work reports (export: files, analysis: audio
    frames, render: thousandths of the output), with `total == 0` while unknown. `message` is
    the current item. `result` is JSON-safe: a partial summary the work published while running
    (`JobContext.set_result`), replaced by the summary of its return value when it finishes.
    Timestamps are `time.time()` seconds.
    """

    id: str
    kind: str
    state: JobState
    label: str | None = None
    completed: float = 0
    total: float = 0
    message: str | None = None
    result: Any = None
    error: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    owner: str | None = None  # the id of the principal that submitted it (see identity.py); None: no request
    error_code: str | None = None  # the failure's exception class name, set with `error`
    # The failure in the error catalogue's terms ({code, message, fix}, docs/errors.md), set with `error`.
    error_info: dict[str, str] | None = None
    group: str | None = None  # an id shared by jobs submitted together (a render batch); None: on its own

    @property
    def finished(self) -> bool:
        return self.state in FINISHED_STATES

    @property
    def fraction(self) -> float:
        """Progress as 0.0..1.0 (0.0 while the total is unknown, 1.0 once done)."""
        if self.state == "done":
            return 1.0
        return min(1.0, self.completed / self.total) if self.total > 0 else 0.0


@dataclass(frozen=True)
class JobSubmitted:
    job: JobInfo


@dataclass(frozen=True)
class JobProgress:
    """Anything that changed without a state change: progress, message, partial result, a cancel request."""

    job: JobInfo


@dataclass(frozen=True)
class JobStateChanged:
    job: JobInfo
    previous: JobState


JobEvent = JobSubmitted | JobProgress | JobStateChanged
JobListener = Callable[[JobEvent], None]


@runtime_checkable
class CancelToken(Protocol):
    """What engine code needs to be cancellable; `JobContext` is one."""

    def is_cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None:
        """Raise `JobCancelledError` if cancellation was requested."""
        ...

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Run `callback` when cancellation is requested (at once if it already was), on the
        canceller's thread; it must be quick and must not raise. Returns a function that
        removes the callback again."""
        ...


@runtime_checkable
class JobQueue(Protocol):
    """Submit work, then ask about it by id. Implementations decide where the work runs."""

    def submit(
        self,
        kind: str,
        work: Callable[[JobContext], Any],
        *,
        label: str | None = None,
        total: int = 0,
        owner: str | None = None,
        group: str | None = None,
    ) -> str:
        """Queue `work`, to be called with a `JobContext`, and return the job's opaque id.
        `total` is the initial progress total (0 if unknown yet). `group` tags jobs submitted together."""
        ...

    def record_done(
        self, kind: str, *, result: Any = None, label: str | None = None, total: int = 0, owner: str | None = None
    ) -> str:
        """Register a job that is already `done` (nothing to run, e.g. the result was cached),
        so a client's submit-then-poll flow is the same either way."""
        ...

    def record_failed(
        self,
        kind: str,
        *,
        error: str,
        error_code: str | None = None,
        error_info: dict[str, str] | None = None,
        label: str | None = None,
        total: int = 0,
        owner: str | None = None,
        group: str | None = None,
    ) -> str:
        """Register a job that has already failed (a request rejected before it could run), so a batch
        submit returns one job id per request, in order, whether or not each one could start."""
        ...

    def concurrency(self, kind: str) -> int:
        """How many jobs of this kind run at once at most."""
        ...

    def status(self, job_id: str) -> JobInfo:
        """The job's current snapshot. `UnknownJobError` for an unknown id."""
        ...

    def cancel(self, job_id: str) -> JobInfo:
        """Ask the job to stop and return its snapshot. Idempotent. A queued job becomes
        `cancelled` at once and never runs; a running one is flagged (`cancel_requested`) and
        becomes `cancelled` when its work stops; a finished job is left as it is."""
        ...

    def result(self, job_id: str) -> Any:
        """What the work returned. `UnknownJobError`, `JobNotFinishedError`, `JobFailedError`
        (it raised) or `JobCancelledError` otherwise."""
        ...

    def wait(self, job_id: str, timeout: float | None = None) -> JobInfo:
        """Block until the job is finished or `timeout` seconds pass; returns the snapshot."""
        ...

    def list(self, *, kind: str | None = None, state: str | None = None, group: str | None = None) -> list[JobInfo]:
        """Known jobs, newest first, optionally of one kind, state and/or group."""
        ...

    def subscribe(self, listener: JobListener) -> Callable[[], None]:
        """Call `listener` with each `JobEvent` (submitted, progress, state change), in order per
        job; returns a function that unsubscribes. Listeners run on the thread that caused the
        event while the queue is locked, so they must be quick, must not raise, and may call
        `status` but must not wait on other jobs."""
        ...


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return None
    return value


class _Job:
    """The queue's mutable record of one job; only touched under the queue's lock."""

    def __init__(self, info: JobInfo, work: Callable[[JobContext], Any] | None) -> None:
        self.info = info
        self.work = work
        self.value: Any = None
        self.callbacks: list[Callable[[], None]] = []


class JobContext:
    """The handle a work function gets: report progress, check for and react to cancellation."""

    def __init__(self, queue: InProcessJobQueue, job: _Job) -> None:
        self._queue = queue
        self._job = job

    @property
    def job_id(self) -> str:
        return self._job.info.id

    @property
    def cancelled(self) -> bool:
        return self.is_cancelled()

    def is_cancelled(self) -> bool:
        with self._queue._lock:
            return self._job.info.cancel_requested

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise JobCancelledError("job cancelled")

    def progress(self, completed: float, total: float | None = None, message: str | None = None) -> None:
        """Report `completed` out of `total` (unchanged if omitted) and, optionally, the current item."""
        changes: dict[str, Any] = {"completed": completed}
        if total is not None:
            changes["total"] = total
        if message is not None:
            changes["message"] = message
        self._queue._update(self._job, changes)

    def set_result(self, summary: Any) -> None:
        """Publish a JSON-safe partial result (e.g. the files written so far)."""
        self._queue._update(self._job, {"result": _json_safe(summary)})

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self._queue._add_cancel_callback(self._job, callback)


class InProcessJobQueue:
    """Runs jobs on a bounded pool of daemon threads in this process.

    At most `max_workers` jobs run at once; the rest wait as `queued`. `kind_limits` caps how many jobs
    of one kind run at once (renders are capped below the pool size, so they cannot starve a proxy or an
    analysis, and a long queue of them cannot exhaust memory); a job over its kind's cap waits while
    others behind it may run. State lives in memory
    for the life of the process, so ids are only good until restart; the newest
    `max_retained` finished jobs are kept and the oldest forgotten beyond that. With
    `synchronous=True` there are no threads: `submit` runs the work to completion before
    returning, which makes tests deterministic.
    """

    def __init__(
        self,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_retained: int = DEFAULT_MAX_RETAINED,
        *,
        synchronous: bool = False,
        kind_limits: dict[str, int] | None = None,
    ) -> None:
        if max_workers < 1 or max_retained < 1:
            raise ValueError("max_workers and max_retained must be at least 1")
        if any(limit < 1 for limit in (kind_limits or {}).values()):
            raise ValueError("a kind's concurrency limit must be at least 1")
        self._kind_limits = dict(kind_limits or {})
        self._running_by_kind: dict[str, int] = {}
        self._max_workers = max_workers
        self._max_retained = max_retained
        self._synchronous = synchronous
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)  # a job finished, or work was queued
        self._jobs: dict[str, _Job] = {}  # in submission order
        self._pending: deque[_Job] = deque()
        self._finished: deque[str] = deque()  # ids, in the order they finished
        self._workers = 0
        self._idle = 0
        self._closed = False
        self._listeners: list[JobListener] = []

    # --- submitting ---------------------------------------------------------------------

    def submit(
        self,
        kind: str,
        work: Callable[[JobContext], Any],
        *,
        label: str | None = None,
        total: int = 0,
        owner: str | None = None,
        group: str | None = None,
    ) -> str:
        with self._lock:
            if self._closed:
                raise JobError("the job queue is shut down")
            job = self._new_job(kind, work, label, total, owner, group)
            self._emit(JobSubmitted(job.info))
            if not self._synchronous:
                self._pending.append(job)
                if self._idle < len(self._pending) and self._workers < self._max_workers:
                    self._workers += 1
                    threading.Thread(target=self._worker, name=f"job-worker-{self._workers}", daemon=True).start()
                self._changed.notify()
        if self._synchronous:
            self._run(job)
        return job.info.id

    def record_done(
        self, kind: str, *, result: Any = None, label: str | None = None, total: int = 0, owner: str | None = None
    ) -> str:
        with self._lock:
            job = self._new_job(kind, None, label, total, owner)
            self._emit(JobSubmitted(job.info))
            now = time.time()
            job.value = result
            self._set_state(job, "done", started_at=now, finished_at=now, completed=total, result=_json_safe(result))
        return job.info.id

    def record_failed(
        self,
        kind: str,
        *,
        error: str,
        error_code: str | None = None,
        error_info: dict[str, str] | None = None,
        label: str | None = None,
        total: int = 0,
        owner: str | None = None,
        group: str | None = None,
    ) -> str:
        with self._lock:
            job = self._new_job(kind, None, label, total, owner, group)
            self._emit(JobSubmitted(job.info))
            self._set_state(
                job, "error", finished_at=time.time(), error=error, error_code=error_code, error_info=error_info
            )
        return job.info.id

    def concurrency(self, kind: str) -> int:
        """At most this many jobs of `kind` run at once (the pool size if the kind has no cap of its own)."""
        return min(self._kind_limits.get(kind, self._max_workers), self._max_workers)

    def _new_job(
        self,
        kind: str,
        work: Callable[[JobContext], Any] | None,
        label: str | None,
        total: int,
        owner: str | None,
        group: str | None = None,
    ) -> _Job:
        info = JobInfo(
            id=uuid.uuid4().hex,
            kind=kind,
            state="queued",
            label=label,
            total=total,
            created_at=time.time(),
            owner=owner,
            group=group,
        )
        job = _Job(info, work)
        self._jobs[info.id] = job
        return job

    # --- asking -------------------------------------------------------------------------

    def _get(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise UnknownJobError(f"unknown job id {job_id!r}")
        return job

    def status(self, job_id: str) -> JobInfo:
        with self._lock:
            return self._get(job_id).info

    def list(self, *, kind: str | None = None, state: str | None = None, group: str | None = None) -> list[JobInfo]:
        with self._lock:
            infos = [j.info for j in reversed(self._jobs.values())]
        return [
            i
            for i in infos
            if (kind is None or i.kind == kind)
            and (state is None or i.state == state)
            and (group is None or i.group == group)
        ]

    def result(self, job_id: str) -> Any:
        with self._lock:
            job = self._get(job_id)
            state = job.info.state
            if state == "done":
                return job.value
            if state == "error":
                raise JobFailedError(job.info.error or "job failed")
            if state == "cancelled":
                raise JobCancelledError("job cancelled")
            raise JobNotFinishedError(f"job {job_id} is still {state}")

    def wait(self, job_id: str, timeout: float | None = None) -> JobInfo:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            job = self._get(job_id)
            while not job.info.finished:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._changed.wait(remaining)
            return job.info

    def subscribe(self, listener: JobListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    # --- cancelling ---------------------------------------------------------------------

    def cancel(self, job_id: str) -> JobInfo:
        with self._lock:
            job = self._get(job_id)
            info = job.info
            if info.finished or info.cancel_requested and info.state == "running":
                return info
            if info.state == "queued":
                try:
                    self._pending.remove(job)
                except ValueError:
                    pass
                self._set_state(job, "cancelled", finished_at=time.time(), cancel_requested=True)
                return job.info
            callbacks = list(job.callbacks)
            self._update(job, {"cancel_requested": True})
            snapshot = job.info
        for callback in callbacks:
            _call_quietly(callback)
        return snapshot

    def shutdown(self) -> None:
        """Cancel everything queued or running (terminating any ffmpeg) and refuse new work.
        Does not wait for the threads, which are daemons."""
        with self._lock:
            self._closed = True
            ids = [j.info.id for j in self._jobs.values() if not j.info.finished]
        for job_id in ids:
            self.cancel(job_id)
        with self._lock:
            self._changed.notify_all()

    def _add_cancel_callback(self, job: _Job, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            already = job.info.cancel_requested
            if not already:
                job.callbacks.append(callback)
        if already:
            _call_quietly(callback)

        def remove() -> None:
            with self._lock:
                if callback in job.callbacks:
                    job.callbacks.remove(callback)

        return remove

    # --- running ------------------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            with self._changed:
                self._idle += 1
                while (job := self._take_runnable()) is None and not self._closed:
                    self._changed.wait()
                self._idle -= 1
                if job is None:
                    self._workers -= 1
                    return
                # Claimed under the lock, before the job is `running`, so two workers cannot both take the last slot.
                self._running_by_kind[job.info.kind] = self._running_by_kind.get(job.info.kind, 0) + 1
            try:
                self._run(job)
            finally:
                with self._changed:
                    self._running_by_kind[job.info.kind] -= 1
                    self._changed.notify_all()  # a job of this kind that was over its cap can start now

    def _take_runnable(self) -> _Job | None:
        """The oldest queued job whose kind is under its concurrency cap, removed from the queue (lock held)."""
        for job in self._pending:
            limit = self._kind_limits.get(job.info.kind)
            if limit is None or self._running_by_kind.get(job.info.kind, 0) < limit:
                self._pending.remove(job)
                return job
        return None

    def _run(self, job: _Job) -> None:
        with self._lock:
            if job.info.state != "queued":
                return  # cancelled while it waited
            self._set_state(job, "running", started_at=time.time())
        assert job.work is not None
        outcome: JobState
        value: Any = None
        error: str | None = None
        error_code: str | None = None
        error_info: dict[str, str] | None = None
        try:
            value = job.work(JobContext(self, job))
            outcome = "done"
        except JobCancelledError:
            outcome = "cancelled"
        except Exception as exc:
            log.info("job %s (%s) failed", job.info.id, job.info.kind, exc_info=True)
            outcome = "error"
            error = str(exc) or type(exc).__name__
            error_code = type(exc).__name__
            info = describe(exc)
            error_info = {"code": info.code, "message": error, "fix": info.fix}
        with self._lock:
            if outcome == "error" and job.info.cancel_requested:
                outcome = "cancelled"  # stopping it (e.g. killing ffmpeg) is what made it fail
            changes: dict[str, Any] = {"finished_at": time.time()}
            if outcome == "done":
                job.value = value
                summary = _json_safe(value)
                if summary is not None:
                    changes["result"] = summary
            elif outcome == "error":
                changes["error"] = error
                changes["error_code"] = error_code
                changes["error_info"] = error_info
            self._set_state(job, outcome, **changes)

    # --- shared bookkeeping (call with the lock held) ------------------------------------

    def _update(self, job: _Job, changes: dict[str, Any]) -> None:
        with self._lock:
            new = replace(job.info, **changes)
            if new == job.info:
                return
            job.info = new
            self._emit(JobProgress(new))

    def _set_state(self, job: _Job, state: JobState, **changes: Any) -> None:
        previous = job.info.state
        job.info = replace(job.info, state=state, **changes)
        if state in FINISHED_STATES:
            job.callbacks.clear()
            self._finished.append(job.info.id)
            while len(self._finished) > self._max_retained:
                self._jobs.pop(self._finished.popleft(), None)
        self._emit(JobStateChanged(job.info, previous))
        self._changed.notify_all()

    def _emit(self, event: JobEvent) -> None:
        for listener in list(self._listeners):
            _call_quietly(listener, event)


def _call_quietly(fn: Callable[..., None], *args: Any) -> None:
    try:
        fn(*args)
    except Exception:
        log.exception("job callback failed")
