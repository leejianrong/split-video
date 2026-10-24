"""The in-process job queue: lifecycle, cancellation, retention and events, without sleeping."""

import subprocess
import sys
import threading
import time
from unittest import mock

import pytest

from split_video.ffmpeg import run_cancellable, stop_on_cancel
from split_video.jobs import (
    InProcessJobQueue,
    JobCancelledError,
    JobFailedError,
    JobNotFinishedError,
    JobProgress,
    JobQueue,
    JobStateChanged,
    JobSubmitted,
    UnknownJobError,
)


@pytest.fixture
def queue():
    q = InProcessJobQueue(max_workers=2)
    yield q
    q.shutdown()


def gate_work():
    """Work that starts, tells the test, then blocks until released (or cancelled)."""
    started, release = threading.Event(), threading.Event()

    def work(ctx):
        started.set()
        while not release.wait(0.01):
            ctx.raise_if_cancelled()
        return "finished"

    return work, started, release


def test_the_queue_satisfies_the_protocol():
    assert isinstance(InProcessJobQueue(), JobQueue)


def test_synchronous_queue_runs_the_work_before_submit_returns():
    q = InProcessJobQueue(synchronous=True)
    job_id = q.submit("export", lambda ctx: {"n": 1}, label="x", total=3)
    info = q.status(job_id)
    assert (info.state, info.kind, info.label, info.total) == ("done", "export", "x", 3)
    assert info.result == {"n": 1} and q.result(job_id) == {"n": 1}
    assert info.created_at <= info.started_at <= info.finished_at
    assert info.fraction == 1.0 and info.finished


def test_threaded_job_goes_queued_running_done_and_result_is_available_by_id(queue):
    work, started, release = gate_work()
    job_id = queue.submit("render", work)
    assert started.wait(5)
    assert queue.status(job_id).state == "running"
    with pytest.raises(JobNotFinishedError):
        queue.result(job_id)
    release.set()
    assert queue.wait(job_id, 5).state == "done"
    assert queue.result(job_id) == "finished"
    assert queue.status(job_id).state == "done"  # still queryable after completion


def test_progress_message_and_partial_result_are_visible_while_running(queue):
    reached, release = threading.Event(), threading.Event()

    def work(ctx):
        ctx.progress(2, total=10, message="b.mp4")
        ctx.set_result({"files": ["a.mp4"]})
        reached.set()
        release.wait(5)
        return {"files": ["a.mp4", "b.mp4"]}

    job_id = queue.submit("export", work)
    assert reached.wait(5)
    info = queue.status(job_id)
    assert (info.completed, info.total, info.message, info.result) == (2, 10, "b.mp4", {"files": ["a.mp4"]})
    assert info.fraction == pytest.approx(0.2)
    release.set()
    assert queue.wait(job_id, 5).result == {"files": ["a.mp4", "b.mp4"]}


def test_a_failing_job_is_error_with_its_message_and_result_raises(queue):
    def work(ctx):
        raise ValueError("boom")

    job_id = queue.submit("analysis", work)
    info = queue.wait(job_id, 5)
    assert (info.state, info.error) == ("error", "boom")
    with pytest.raises(JobFailedError, match="boom"):
        queue.result(job_id)


def test_unknown_job_id_raises_everywhere(queue):
    for call in (queue.status, queue.cancel, queue.result, queue.wait):
        with pytest.raises(UnknownJobError):
            call("nope")


def test_a_non_json_return_value_is_kept_for_result_but_not_in_the_summary():
    q = InProcessJobQueue(synchronous=True)
    obj = object()
    job_id = q.submit("export", lambda ctx: obj)
    assert q.result(job_id) is obj and q.status(job_id).result is None


def test_cancelling_a_running_job_flags_it_runs_callbacks_and_ends_cancelled(queue):
    seen = []
    ready = threading.Event()

    def work(ctx):
        ctx.on_cancel(lambda: seen.append("callback"))
        ready.set()
        while True:
            ctx.raise_if_cancelled()
            threading.Event().wait(0.005)

    job_id = queue.submit("render", work)
    assert ready.wait(5)
    snapshot = queue.cancel(job_id)
    assert snapshot.cancel_requested and snapshot.state in ("running", "cancelled")
    info = queue.wait(job_id, 5)
    assert info.state == "cancelled" and info.finished_at is not None and seen == ["callback"]
    with pytest.raises(JobCancelledError):
        queue.result(job_id)


def test_cancel_is_idempotent_and_a_no_op_on_finished_jobs(queue):
    work, started, release = gate_work()
    job_id = queue.submit("render", work)
    assert started.wait(5)
    first = queue.cancel(job_id)
    second = queue.cancel(job_id)
    assert first.cancel_requested and second.cancel_requested
    assert queue.wait(job_id, 5).state == "cancelled"
    assert queue.cancel(job_id).state == "cancelled"

    done = InProcessJobQueue(synchronous=True)
    finished = done.submit("export", lambda ctx: 1)
    assert done.cancel(finished).state == "done"  # the final state, untouched
    failed = done.submit("export", lambda ctx: 1 / 0)
    assert done.cancel(failed).state == "error"


def test_cancelling_a_queued_job_cancels_it_at_once_and_it_never_runs():
    q = InProcessJobQueue(max_workers=1)
    work, started, release = gate_work()
    ran = []
    first = q.submit("render", work)
    assert started.wait(5)
    second = q.submit("render", lambda ctx: ran.append(1))
    assert q.status(second).state == "queued"
    info = q.cancel(second)
    assert info.state == "cancelled" and info.started_at is None and info.finished_at is not None
    release.set()
    assert q.wait(first, 5).state == "done"
    q.shutdown()
    assert ran == [] and q.status(second).state == "cancelled"


def test_jobs_queue_when_the_pool_is_busy_and_run_in_order():
    q = InProcessJobQueue(max_workers=1)
    order = []
    work, started, release = gate_work()
    first = q.submit("render", work)
    assert started.wait(5)
    ids = [q.submit("render", lambda ctx, n=n: order.append(n)) for n in range(3)]
    assert [q.status(i).state for i in ids] == ["queued"] * 3
    release.set()
    for i in (first, *ids):
        assert q.wait(i, 5).state == "done"
    assert order == [0, 1, 2]
    q.shutdown()


def test_at_most_max_workers_jobs_run_at_once():
    q = InProcessJobQueue(max_workers=2)
    lock = threading.Lock()
    running = peak = 0
    release = threading.Event()

    def work(ctx):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(5)
        with lock:
            running -= 1

    ids = [q.submit("render", work) for _ in range(6)]
    # Two start, four wait; release lets them drain.
    deadline_hit = threading.Event()
    while not deadline_hit.wait(0.01):
        with lock:
            if running == 2:
                break
    release.set()
    for i in ids:
        q.wait(i, 5)
    assert peak == 2
    q.shutdown()


def test_record_done_is_a_finished_job_from_the_start(queue):
    job_id = queue.record_done("analysis", result={"cached": True}, total=0)
    info = queue.status(job_id)
    assert info.state == "done" and info.started_at is not None and info.result == {"cached": True}
    assert queue.result(job_id) == {"cached": True}


def test_only_the_oldest_finished_jobs_are_evicted_past_the_cap():
    q = InProcessJobQueue(max_workers=1, max_retained=3, synchronous=True)
    ids = [q.submit("export", lambda ctx, n=n: n) for n in range(5)]
    assert [i.id for i in q.list()] == ids[:1:-1] and len(q.list()) == 3
    for gone in ids[:2]:
        with pytest.raises(UnknownJobError):
            q.status(gone)
    assert q.result(ids[4]) == 4

    threaded = InProcessJobQueue(max_workers=1, max_retained=1)
    work, started, release = gate_work()
    running = threaded.submit("render", work)
    assert started.wait(5)
    done1 = threaded.record_done("export")
    done2 = threaded.record_done("export")
    assert threaded.status(running).state == "running"  # an active job is never evicted
    assert threaded.status(done2).state == "done"
    with pytest.raises(UnknownJobError):
        threaded.status(done1)
    release.set()
    threaded.shutdown()


def test_list_is_newest_first_and_filters_by_kind_and_state():
    q = InProcessJobQueue(synchronous=True)
    a = q.submit("export", lambda ctx: 1)
    b = q.submit("render", lambda ctx: 1 / 0)
    c = q.submit("export", lambda ctx: 1)
    assert [i.id for i in q.list()] == [c, b, a]
    assert [i.id for i in q.list(kind="export")] == [c, a]
    assert [i.id for i in q.list(state="error")] == [b]
    assert q.list(kind="analysis") == []


def test_events_arrive_in_order_for_a_job():
    q = InProcessJobQueue(synchronous=True)
    events = []
    unsubscribe = q.subscribe(events.append)

    def work(ctx):
        ctx.progress(1, total=2, message="one")
        ctx.progress(1, total=2, message="one")  # no change, no event
        ctx.progress(2)

    job_id = q.submit("export", work, total=2)
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["JobSubmitted", "JobStateChanged", "JobProgress", "JobProgress", "JobStateChanged"]
    assert isinstance(events[0], JobSubmitted) and events[0].job.state == "queued"
    assert isinstance(events[1], JobStateChanged) and (events[1].previous, events[1].job.state) == ("queued", "running")
    assert [e.job.completed for e in events if isinstance(e, JobProgress)] == [1, 2]
    assert events[-1].job.state == "done" and events[-1].job.id == job_id
    unsubscribe()
    q.submit("export", lambda ctx: None)
    assert len(events) == 5


def test_a_failing_listener_does_not_break_the_queue():
    q = InProcessJobQueue(synchronous=True)

    def bad(event):
        raise RuntimeError("listener bug")

    q.subscribe(bad)
    job_id = q.submit("export", lambda ctx: 7)
    assert q.result(job_id) == 7


def test_cancel_request_event_and_final_event_for_a_running_job(queue):
    events = []
    queue.subscribe(events.append)
    work, started, release = gate_work()
    job_id = queue.submit("render", work)
    assert started.wait(5)
    queue.cancel(job_id)
    queue.wait(job_id, 5)
    mine = [e for e in events if e.job.id == job_id]
    assert any(isinstance(e, JobProgress) and e.job.cancel_requested for e in mine)
    assert isinstance(mine[-1], JobStateChanged) and mine[-1].job.state == "cancelled"


def test_work_that_fails_after_a_cancel_request_counts_as_cancelled(queue):
    ready = threading.Event()

    def work(ctx):
        ready.set()
        while not ctx.cancelled:
            threading.Event().wait(0.005)
        raise RuntimeError("ffmpeg died because we killed it")

    job_id = queue.submit("render", work)
    assert ready.wait(5)
    queue.cancel(job_id)
    assert queue.wait(job_id, 5).state == "cancelled"


def test_callback_registered_after_cancel_runs_at_once(queue):
    seen = []
    ready, cancelled = threading.Event(), threading.Event()

    def work(ctx):
        ready.set()
        cancelled.wait(5)
        ctx.on_cancel(lambda: seen.append("late"))
        ctx.raise_if_cancelled()

    job_id = queue.submit("render", work)
    assert ready.wait(5)
    queue.cancel(job_id)
    cancelled.set()
    assert queue.wait(job_id, 5).state == "cancelled" and seen == ["late"]


def test_many_jobs_from_many_threads(queue):
    results = {}

    def submit_some(offset):
        for n in range(25):
            results[offset + n] = queue.submit("export", lambda ctx, v=offset + n: v)

    threads = [threading.Thread(target=submit_some, args=(i * 100,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 200
    for value, job_id in results.items():
        assert queue.wait(job_id, 10).state == "done"
        assert queue.result(job_id) == value
    assert len(queue.list()) == 200


def test_shutdown_cancels_queued_and_running_jobs_and_refuses_new_ones():
    q = InProcessJobQueue(max_workers=1)
    work, started, release = gate_work()
    first = q.submit("render", work)
    assert started.wait(5)
    second = q.submit("render", lambda ctx: 1)
    q.shutdown()
    assert q.wait(first, 5).state == "cancelled" and q.status(second).state == "cancelled"
    with pytest.raises(Exception, match="shut down"):
        q.submit("render", lambda ctx: 1)


# --- the ffmpeg helpers' cancellation, with a stand-in child process -----------------------------


def _sleeper():
    return [sys.executable, "-c", "import time; time.sleep(60)"]


def test_run_cancellable_terminates_the_process_and_raises(queue):
    procs = []
    real_popen = subprocess.Popen

    def spy(*a, **k):
        procs.append(real_popen(*a, **k))
        return procs[-1]

    ready = threading.Event()

    def work(ctx):
        ready.set()
        run_cancellable(_sleeper(), ctx)

    with mock.patch("split_video.ffmpeg.subprocess.Popen", spy):
        job_id = queue.submit("export", work)
        assert ready.wait(5)
        while not procs:
            threading.Event().wait(0.005)
        queue.cancel(job_id)
        assert queue.wait(job_id, 10).state == "cancelled"
    assert procs[0].poll() is not None  # reaped, not left running


def test_stop_on_cancel_kills_a_process_that_ignores_sigterm(queue, monkeypatch):
    monkeypatch.setattr("split_video.ffmpeg.KILL_GRACE_SECONDS", 0.3)
    import split_video.ffmpeg as ffmpeg_module

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "up"  # the handler is installed
    ready = threading.Event()

    def work(ctx):
        stop_on_cancel(proc, ctx)
        ready.set()
        proc.wait()

    job_id = queue.submit("export", work)
    assert ready.wait(5)
    queue.cancel(job_id)
    assert queue.wait(job_id, 10).state == "done"  # the work only waited for the process
    assert proc.returncode is not None
    assert ffmpeg_module.KILL_GRACE_SECONDS == 0.3


def test_a_kind_limit_caps_that_kind_and_other_kinds_still_run():
    q = InProcessJobQueue(max_workers=3, kind_limits={"render": 1})
    try:
        release = threading.Event()
        running = []

        def work(label):
            def run(ctx):
                running.append(label)
                release.wait(10)

            return run

        a = q.submit("render", work("r1"))
        b = q.submit("render", work("r2"))
        c = q.submit("proxy", work("p1"))
        deadline = time.monotonic() + 5
        while len(running) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sorted(running) == ["p1", "r1"]  # r2 waits for its kind's slot although a worker is free
        assert q.status(b).state == "queued" and q.status(c).state == "running"
        release.set()
        for job in (a, b, c):
            assert q.wait(job, 5).state == "done"
        assert q.concurrency("render") == 1 and q.concurrency("proxy") == 3
    finally:
        q.shutdown()


def test_record_failed_and_group_and_error_code():
    q = InProcessJobQueue(synchronous=True)
    failed = q.record_failed("render", error="nope", error_code="TimelineError", label="x.mp4", group="g1")
    boom = q.submit("render", lambda ctx: 1 / 0, group="g1")
    other = q.submit("render", lambda ctx: None)
    assert (q.status(failed).state, q.status(failed).error, q.status(failed).error_code) == (
        "error",
        "nope",
        "TimelineError",
    )
    assert q.status(boom).error_code == "ZeroDivisionError"
    assert {i.id for i in q.list(group="g1")} == {failed, boom} and other not in {i.id for i in q.list(group="g1")}
    with pytest.raises(JobFailedError, match="nope"):
        q.result(failed)


def test_render_concurrency_setting_is_bounded(monkeypatch):
    from split_video.jobs import MAX_RENDER_CONCURRENCY, check_render_concurrency, render_concurrency_from_env

    monkeypatch.delenv("SPLIT_VIDEO_RENDER_CONCURRENCY", raising=False)
    assert render_concurrency_from_env() == 2
    monkeypatch.setenv("SPLIT_VIDEO_RENDER_CONCURRENCY", "3")
    assert render_concurrency_from_env() == 3
    for bad in ("0", "99", "many"):
        monkeypatch.setenv("SPLIT_VIDEO_RENDER_CONCURRENCY", bad)
        with pytest.raises(ValueError, match="between 1 and"):
            render_concurrency_from_env()
    with pytest.raises(ValueError):
        check_render_concurrency(MAX_RENDER_CONCURRENCY + 1)
    with pytest.raises(ValueError):
        InProcessJobQueue(kind_limits={"render": 0})
