"""The render queue on the in-memory stores, with ffmpeg replaced by a fake that can wait, fail or be cancelled."""

import threading
import time

import pytest

from split_video import render, render_queue, timeline_service
from split_video.ffmpeg import ExtractError, MediaInfo
from split_video.identity import Principal
from split_video.jobs import RENDER, InProcessJobQueue, UnknownJobError
from split_video.media_store import InMemoryMediaStore
from split_video.store import InMemoryProjectStore

VIDEO = MediaInfo(duration=30.0, width=320, height=240, has_audio=True)


@pytest.fixture
def media():
    store = InMemoryMediaStore()
    store.add("a.mp4", b"a", info=VIDEO)
    return store


@pytest.fixture
def store(media):
    store = InMemoryProjectStore()
    for name in ("reel", "short1", "short2", "short3"):
        timeline_service.create_project(store, name)
        timeline_service.add_clip(store, media, name, "a.mp4")
    timeline_service.create_project(store, "empty")
    return store


@pytest.fixture
def jobs():
    queue = InProcessJobQueue(max_workers=3, kind_limits={RENDER: 2})
    yield queue
    queue.shutdown()


class FakeFfmpeg:
    """Stands in for `render._run_ffmpeg`: records starts and concurrency, and obeys per-output instructions."""

    def __init__(self, media):
        self.media = media
        self.lock = threading.Lock()
        self.started: list[str] = []
        self.running = 0
        self.max_running = 0
        self.fail: set[str] = set()
        self.hold: set[str] = set()  # these outputs wait until cancelled (or released)
        self.release = threading.Event()

    def __call__(self, plan, inputs, out_path, total, progress, cancel=None, preset=None, stall_timeout=None):
        name = out_path.rsplit("/", 1)[-1]
        with self.lock:
            self.started.append(name)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            self.media.write_bytes(name, b"partial")
            if name in self.fail:
                raise ExtractError(f"ffmpeg failed rendering '{name}'")
            if name in self.hold:
                while not self.release.is_set():
                    cancel.raise_if_cancelled()
                    time.sleep(0.01)
            else:
                time.sleep(0.15)
            if progress:
                progress(1.0)
        finally:
            with self.lock:
                self.running -= 1


@pytest.fixture
def ffmpeg(media, monkeypatch):
    fake = FakeFfmpeg(media)
    monkeypatch.setattr(render, "_run_ffmpeg", fake)
    return fake


def _wait(jobs, batch):
    for job in batch["jobs"]:
        jobs.wait(job["job_id"], 15)
    return render_queue.batch_status(jobs, batch["batch_id"])


def _states(batch):
    return [j["state"] for j in batch["jobs"]]


def test_three_renders_queue_and_finish_with_at_most_two_at_a_time(store, media, jobs, ffmpeg):
    batch = render_queue.submit_batch(
        jobs,
        store,
        media,
        [
            {"project": "reel", "preset": "youtube-1080p"},
            {"project": "short1", "preset": "vertical-1080", "output": "s1.mp4"},
            {"project": "short2", "preset": "vertical-1080", "output": "s2.mp4"},
        ],
    )
    assert batch["total"] == 3 and batch["concurrency"] == 2 and [j["index"] for j in batch["jobs"]] == [1, 2, 3]
    assert [j["output"] for j in batch["jobs"]] == ["reel-render.mp4", "s1.mp4", "s2.mp4"]
    done = _wait(jobs, batch)
    assert _states(done) == ["done"] * 3 and done["finished"] and done["counts"]["done"] == 3
    assert ffmpeg.max_running == 2  # the cap held, and was reached
    assert done["jobs"][1]["result"]["preset"] == "vertical-1080"
    assert done["jobs"][1]["result"]["width"] == 1080 and done["jobs"][1]["result"]["height"] == 1920
    assert all(media.exists(o) for o in ("reel-render.mp4", "s1.mp4", "s2.mp4"))
    assert done["jobs"][0]["progress"] == 1.0 and done["jobs"][0]["error"] is None


def test_a_failing_render_ends_failed_with_a_structured_error_and_siblings_finish(store, media, jobs, ffmpeg):
    ffmpeg.fail.add("bad.mp4")
    batch = render_queue.submit_batch(
        jobs,
        store,
        media,
        [{"project": "reel"}, {"project": "short1", "output": "bad.mp4"}, {"project": "short2", "output": "ok.mp4"}],
    )
    done = _wait(jobs, batch)
    assert _states(done) == ["done", "failed", "done"]
    assert done["counts"] == {"queued": 0, "running": 0, "done": 2, "failed": 1, "cancelled": 0}
    error = done["jobs"][1]["error"]
    assert (error["code"], error["message"]) == ("ffmpeg.failed", "ffmpeg failed rendering 'bad.mp4'")
    assert "ffprobe" in error["fix"]
    assert done["jobs"][1]["result"] is None
    assert not media.exists("bad.mp4")  # the partial file is gone
    assert media.exists("reel-render.mp4") and media.exists("ok.mp4")


def test_requests_that_cannot_start_come_back_failed_in_place_and_do_not_stop_the_batch(store, media, jobs, ffmpeg):
    media.write_bytes("taken.mp4", b"keep me")
    batch = render_queue.submit_batch(
        jobs,
        store,
        media,
        [
            {"project": "reel", "output": "one.mp4"},
            {"project": "nope"},
            {"project": "short1", "preset": "no-such-preset"},
            {"project": "short1", "fit": "stretch"},
            {"project": "short2", "output": "taken.mp4"},
            {"project": "short2", "output": "one.mp4"},  # same output as request 1
            {"project": "empty", "output": "empty.mp4"},
            {"project": "short3", "bogus": 1},
            {"project": "short3", "captions": "no-such-style"},
            "not an object",
            {"project": "short3", "output": "last.mp4"},
        ],
    )
    done = _wait(jobs, batch)
    assert _states(done) == ["done"] + ["failed"] * 9 + ["done"]
    codes = [j["error"]["code"] for j in done["jobs"][1:10]]
    assert codes[0] == "timeline.not_found" and "render.output_exists" in codes and "render.request_invalid" in codes
    assert "already the output of another render" in done["jobs"][5]["error"]["message"]
    assert "unknown render option" in done["jobs"][7]["error"]["message"]
    assert [j["output"] for j in done["jobs"]][1] == "nope-render.mp4"
    assert media.read("taken.mp4") == b"keep me"  # an existing file is never touched without overwrite
    assert ffmpeg.started.count("one.mp4") == 1 and "last.mp4" in ffmpeg.started


def test_cancelling_a_batch_stops_the_running_renders_and_queued_ones_never_start(store, media, jobs, ffmpeg):
    ffmpeg.hold |= {"reel-render.mp4", "s1.mp4"}
    batch = render_queue.submit_batch(
        jobs,
        store,
        media,
        [{"project": "reel"}, {"project": "short1", "output": "s1.mp4"}, {"project": "short2", "output": "s2.mp4"}],
    )
    deadline = time.monotonic() + 10
    while ffmpeg.running < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    status = render_queue.batch_status(jobs, batch["batch_id"])
    assert _states(status) == ["running", "running", "queued"]
    cancelled = render_queue.cancel_renders(jobs, batch_id=batch["batch_id"])
    assert _states(cancelled) == ["cancelled"] * 3 and cancelled["finished"]
    assert ffmpeg.started == ["reel-render.mp4", "s1.mp4"]  # the third never started
    assert not media.exists("reel-render.mp4") and not media.exists("s1.mp4") and not media.exists("s2.mp4")
    assert render_queue.cancel_renders(jobs, batch_id=batch["batch_id"]) == cancelled  # idempotent


def test_cancelling_one_queued_render_leaves_the_others_running(store, media, jobs, ffmpeg):
    ffmpeg.hold |= {"reel-render.mp4", "s1.mp4"}
    batch = render_queue.submit_batch(
        jobs,
        store,
        media,
        [{"project": "reel"}, {"project": "short1", "output": "s1.mp4"}, {"project": "short2", "output": "s2.mp4"}],
    )
    queued = batch["jobs"][2]["job_id"]
    after = render_queue.cancel_renders(jobs, job_id=queued)
    assert _states(after) == ["running", "running", "cancelled"] or _states(after) == ["queued", "running", "cancelled"]
    ffmpeg.release.set()
    done = _wait(jobs, batch)
    assert _states(done) == ["done", "done", "cancelled"]
    assert "s2.mp4" not in ffmpeg.started and not media.exists("s2.mp4")


def test_a_slot_freed_by_a_cancel_starts_the_next_queued_render(store, media, jobs, ffmpeg):
    ffmpeg.hold |= {"reel-render.mp4", "s1.mp4"}
    batch = render_queue.submit_batch(
        jobs,
        store,
        media,
        [{"project": "reel"}, {"project": "short1", "output": "s1.mp4"}, {"project": "short2", "output": "s2.mp4"}],
    )
    deadline = time.monotonic() + 10
    while ffmpeg.running < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    render_queue.cancel_renders(jobs, job_id=batch["jobs"][0]["job_id"])
    jobs.wait(batch["jobs"][2]["job_id"], 15)
    ffmpeg.release.set()
    done = _wait(jobs, batch)
    assert _states(done) == ["cancelled", "done", "done"]


def test_a_failure_before_ffmpeg_starts_never_deletes_an_existing_output(store, media, jobs, ffmpeg, monkeypatch):
    media.write_bytes("keep.mp4", b"old")

    def boom(*args, **kwargs):
        raise ExtractError("boom")

    monkeypatch.setattr(render, "build_plan", boom)
    batch = render_queue.submit_batch(
        jobs, store, media, [{"project": "reel", "output": "keep.mp4", "overwrite": True}]
    )
    done = _wait(jobs, batch)
    assert _states(done) == ["failed"] and media.read("keep.mp4") == b"old"


def test_batches_are_checked_as_a_whole(store, media, jobs):
    with pytest.raises(render_queue.RenderQueueError, match="empty"):
        render_queue.submit_batch(jobs, store, media, [])
    with pytest.raises(render_queue.RenderQueueError, match="list"):
        render_queue.submit_batch(jobs, store, media, {"project": "reel"})
    with pytest.raises(render_queue.RenderQueueError, match="at most"):
        render_queue.submit_batch(jobs, store, media, [{"project": "reel"}] * (render_queue.MAX_BATCH + 1))
    with pytest.raises(render_queue.UnknownBatchError):
        render_queue.batch_status(jobs, "nope")
    with pytest.raises(render_queue.UnknownBatchError):
        render_queue.cancel_renders(jobs, batch_id="nope")
    with pytest.raises(UnknownJobError):
        render_queue.cancel_renders(jobs, job_id="nope")
    with pytest.raises(render_queue.RenderQueueError, match="exactly one"):
        render_queue.cancel_renders(jobs)


def test_another_principal_cannot_see_or_cancel_a_batch(store, media, jobs, ffmpeg):
    alice, bob = Principal(id="alice", name="Alice", kind="user"), Principal(id="bob", name="Bob", kind="user")
    batch = render_queue.submit_batch(jobs, store, media, [{"project": "reel"}], principal=alice)
    assert render_queue.batch_status(jobs, batch["batch_id"], alice)["total"] == 1
    with pytest.raises(render_queue.UnknownBatchError):
        render_queue.batch_status(jobs, batch["batch_id"], bob)
    with pytest.raises(render_queue.UnknownBatchError):
        render_queue.cancel_renders(jobs, batch_id=batch["batch_id"], viewer=bob)
    with pytest.raises(UnknownJobError):
        render_queue.cancel_renders(jobs, job_id=batch["jobs"][0]["job_id"], viewer=bob)
    assert render_queue.queue_status(jobs, bob)["total"] == 0
    jobs.wait(batch["jobs"][0]["job_id"], 15)
    assert render_queue.queue_status(jobs, alice)["total"] == 1


def test_queue_status_lists_every_render_in_order(store, media, jobs, ffmpeg):
    first = render_queue.submit_batch(jobs, store, media, [{"project": "reel"}])
    second = render_queue.submit_batch(jobs, store, media, [{"project": "short1", "output": "s.mp4"}])
    _wait(jobs, first), _wait(jobs, second)
    overview = render_queue.queue_status(jobs)
    assert overview["batch_id"] is None and [j["output"] for j in overview["jobs"]] == ["reel-render.mp4", "s.mp4"]
