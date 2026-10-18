"""`GET /api/v1/events`: job progress and project changes over server-sent events.

These run a real uvicorn server on an ephemeral port in a background thread and read the stream
with httpx, so disconnects, shutdown and threading behave as they do for a browser. Every read has
a deadline; nothing waits on a live stream without one.
"""

import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

from split_video.editor.app import create_app
from split_video.editor.events import EventBus, EventsConfig
from split_video.editor.schemas import StateParams
from split_video.editor.serve import make_server
from split_video.jobs import InProcessJobQueue
from split_video.local_store import LocalProjectStore
from split_video.store import InMemoryProjectStore

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
FAST = EventsConfig(
    poll_interval=0.05, keepalive_interval=0.2, progress_interval=0.0, idle_linger=0.2, confirm_grace=0.03
)


# --- harness ------------------------------------------------------------------------------------


class Live:
    def __init__(self, root, jobs, config):
        self.root = root
        self.jobs = jobs
        self.app = create_app(root, DEFAULTS, jobs=jobs, events=config)
        self.bus: EventBus = self.app.state.events
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.base = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.server = make_server(self.app, "127.0.0.1", self.sock.getsockname()[1])
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve(sockets=[self.sock])), daemon=True, name="test-uvicorn"
        )
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.01)
        assert self.server.started, "server did not start"
        self.http = httpx.Client(base_url=self.base, timeout=10)
        self.streams: list[Stream] = []

    def stream(self, params=None, headers=None, wait_hello=True):
        s = Stream(self.base, params, headers)
        self.streams.append(s)
        if wait_hello:
            assert s.next(5)["event"] == "hello"
        return s

    def stop(self):
        """Stop the server; returns how long that took."""
        start = time.monotonic()
        self.server.should_exit = True
        self.thread.join(15)
        took = time.monotonic() - start
        assert not self.thread.is_alive(), "server did not stop"
        return took

    def close(self):
        for s in self.streams:
            s.close()
        self.http.close()
        if self.thread.is_alive():
            self.stop()
        self.sock.close()


class Stream:
    """One SSE connection; `next` parses one frame, honoring a deadline."""

    def __init__(self, base, params=None, headers=None):
        self._cm = httpx.stream(
            "GET", f"{base}/api/v1/events", params=params, headers=headers, timeout=httpx.Timeout(5.0)
        )
        self.response = self._cm.__enter__()
        self._lines = self.response.iter_lines()
        self.ended = False

    def next(self, timeout=5.0, keepalives=False):
        """The next frame as a dict (`event`, `id`, `data` parsed, `retry`), or None when the stream ends.
        Keepalive comments are skipped unless `keepalives`, in which case they come as {"comment": ...}."""
        deadline = time.monotonic() + timeout
        frame: dict = {}
        while time.monotonic() < deadline:
            try:
                line = next(self._lines)
            except StopIteration:
                self.ended = True
                return None
            except (httpx.ReadError, httpx.RemoteProtocolError):
                self.ended = True
                return None
            if line == "":
                if frame:
                    return frame
                continue
            if line.startswith(":"):
                if keepalives:
                    return {"comment": line[1:].strip()}
                continue
            field, _, value = line.partition(": ")
            frame[field] = json.loads(value) if field == "data" else value
        raise AssertionError(f"no frame within {timeout}s (got {frame})")

    def until(self, pred, timeout=10.0, name=None):
        """Frames up to and including the first for which `pred(frame)` holds."""
        deadline = time.monotonic() + timeout
        seen = []
        while time.monotonic() < deadline:
            frame = self.next(max(0.1, deadline - time.monotonic()))
            if frame is None:
                raise AssertionError(f"stream ended; saw {seen}")
            seen.append(frame)
            if (name is None or frame["event"] == name) and pred(frame):
                return seen
        raise AssertionError(f"condition not met; saw {seen}")

    def quiet_for(self, seconds):
        """Frames that arrive over `seconds` (keepalives excluded)."""
        deadline = time.monotonic() + seconds
        got = []
        while time.monotonic() < deadline:
            try:
                frame = self.next(deadline - time.monotonic() + 0.05)
            except AssertionError:
                break
            if frame is None:
                break
            got.append(frame)
        return got

    def close(self):
        with contextlib.suppress(httpx.HTTPError, OSError, RuntimeError):
            self._cm.__exit__(None, None, None)


@pytest.fixture
def live(tmp_path):
    made = []

    def make(config=FAST, jobs=None, root=None):
        server = Live(root or tmp_path, jobs or InProcessJobQueue(), config)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


def _jobs_of(frames):
    return [f["data"] for f in frames if f["event"] == "job"]


def _cli(*args):
    result = subprocess.run(
        [sys.executable, "-c", "from split_video.cli import app; app()", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def _wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


# --- job progress -----------------------------------------------------------------------------------


def test_job_progress_arrives_in_order_over_sse(live):
    server = live()
    stream = server.stream()
    release = threading.Event()

    def work(ctx):
        release.wait(5)
        for i in range(1, 4):
            ctx.progress(i, 3, f"item {i}")
        return "ok"

    job_id = server.jobs.submit("export", work, label="demo", total=3)
    release.set()
    frames = stream.until(
        lambda f: f["data"].get("type") == "state" and f["data"]["job"]["state"] == "done", name="job"
    )
    events = [e for e in _jobs_of(frames) if e["job"]["id"] == job_id]
    kinds = [(e["type"], e["job"]["state"], e["job"]["completed"]) for e in events]
    assert kinds[0] == ("submitted", "queued", 0)
    assert ("state", "running", 0) in kinds
    progress = [k[2] for k in kinds if k[0] == "progress"]
    assert progress == [1, 2, 3]
    assert kinds[-1][:2] == ("state", "done")
    assert [e["previous"] for e in events if e["type"] == "state"] == ["queued", "running"]
    # Event ids are increasing integers, and the job payload is the same shape the polling route serves.
    ids = [int(f["id"]) for f in frames]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert events[-1]["job"] == server.http.get(f"/api/v1/jobs/{job_id}").json()
    assert events[-2]["job"]["message"] == "item 3"


def test_a_real_export_streams_progress_then_done(live, three_songs_clip):
    server = live(root=three_songs_clip.parent)
    stream = server.stream()
    segments = [{"start": i * 5.0, "end": i * 5.0 + 4.0} for i in range(3)]
    job_id = server.http.post(
        "/api/v1/export", params={"path": three_songs_clip.name}, json={"segments": segments}
    ).json()["job_id"]
    frames = stream.until(
        lambda f: f["data"].get("type") == "state" and f["data"]["job"]["state"] == "done", 30, name="job"
    )
    events = [e for e in _jobs_of(frames) if e["job"]["id"] == job_id]
    types = [e["type"] for e in events]
    assert types[0] == "submitted" and types[-1] == "state"
    assert any(e["type"] == "progress" and e["job"]["completed"] == 3 for e in events)
    assert events[-1]["job"]["result"]["manifest_path"].endswith("manifest.json")


def test_progress_is_throttled_but_the_last_one_always_arrives(live):
    server = live(EventsConfig(poll_interval=5, keepalive_interval=0.2, progress_interval=0.3))
    stream = server.stream()

    def work(ctx):
        for i in range(1, 201):
            ctx.progress(i, 200)

    job_id = server.jobs.submit("export", work, total=200)
    frames = stream.until(
        lambda f: f["data"].get("type") == "state" and f["data"]["job"]["state"] == "done", name="job"
    )
    events = [e for e in _jobs_of(frames) if e["job"]["id"] == job_id]
    progress = [e["job"]["completed"] for e in events if e["type"] == "progress"]
    assert 1 <= len(progress) <= 4, progress  # 200 updates in a few ms: far fewer frames
    assert progress[-1] == 200 and progress == sorted(progress)
    assert events[-1]["type"] == "state" and events[-2]["type"] == "progress"  # last progress precedes the state


def test_a_throttled_progress_is_delivered_late_not_lost(live):
    server = live(EventsConfig(poll_interval=5, keepalive_interval=5, progress_interval=0.2))
    stream = server.stream()
    go, finish = threading.Event(), threading.Event()

    def work(ctx):
        ctx.progress(1, 10)
        ctx.progress(2, 10)  # inside the interval: held back, then sent by the timer
        go.set()
        finish.wait(5)

    server.jobs.submit("export", work, total=10)
    go.wait(5)
    frames = stream.until(
        lambda f: f["data"].get("type") == "progress" and f["data"]["job"]["completed"] == 2, 3, "job"
    )
    assert frames
    finish.set()


# --- project changes --------------------------------------------------------------------------------


def test_an_in_process_timeline_edit_emits_exactly_one_local_event(live, tmp_path):
    server = live()
    stream = server.stream(params={"kind": "project"})
    assert server.http.post("/api/v1/compose/projects", json={"name": "show"}).status_code == 200
    created = stream.next(5)
    assert created["event"] == "project"
    assert created["data"] == {"kind": "timeline", "project_id": "show", "revision": 0, "source": "local"}
    revision = server.http.post("/api/v1/compose/projects/show/tracks", json={"kind": "audio"}).json()["revision"]
    event = stream.next(5)
    assert event["data"] == {"kind": "timeline", "project_id": "show", "revision": revision, "source": "local"}
    # Several polls later there is still no second (external) event for the same write.
    assert stream.quiet_for(0.5) == []


def test_every_in_process_write_is_one_local_event_even_with_a_fast_watcher(live):
    server = live(EventsConfig(poll_interval=0.01, keepalive_interval=5, confirm_grace=0.03, progress_interval=0))
    stream = server.stream(params={"kind": "project"})
    server.http.post("/api/v1/compose/projects", json={"name": "busy"})
    for _ in range(15):
        assert server.http.post("/api/v1/compose/projects/busy/tracks", json={"kind": "audio"}).status_code == 200
    frames = stream.quiet_for(1.0)
    data = [f["data"] for f in frames]
    assert [d["revision"] for d in data] == list(range(16))
    assert {d["source"] for d in data} == {"local"}


def test_a_split_project_save_emits_a_local_event_with_the_new_revision(live, three_songs_clip):
    server = live(root=three_songs_clip.parent)
    stream = server.stream(params={"kind": "project"})
    path = {"path": three_songs_clip.name}
    segs = {"segments": [{"start": 0.0, "end": 6.0}, {"start": 6.0, "end": 20.0}]}
    assert server.http.post("/api/v1/project", params=path, json=segs).json()["revision"] == 1
    event = stream.next(5)
    assert event["event"] == "project"
    assert event["data"] == {
        "kind": "split",
        "project_id": three_songs_clip.name,
        "revision": 1,
        "source": "local",
    }
    assert stream.quiet_for(0.4) == []


def test_an_edit_from_another_store_instance_is_noticed_as_external(live, tmp_path):
    server = live()
    LocalProjectStore(tmp_path).create_timeline("agent")  # before the stream opens: just the baseline
    stream = server.stream(params={"kind": "project"})
    other = LocalProjectStore(tmp_path)  # a separate store, as another process would have
    from split_video import timeline

    revision = other.update_timeline("agent", lambda p: p)
    assert revision.revision == 1
    event = stream.next(5)
    assert event["data"] == {"kind": "timeline", "project_id": "agent", "revision": 1, "source": "external"}
    other.save_timeline("agent", timeline.empty_project())
    assert stream.next(5)["data"]["revision"] == 2
    assert stream.quiet_for(0.3) == []


def test_a_timeline_created_by_another_process_is_announced(live, tmp_path):
    server = live()
    stream = server.stream(params={"kind": "project"})
    _cli("compose", "new", "fresh", "--root", tmp_path)
    event = stream.next(10)
    assert event["data"] == {"kind": "timeline", "project_id": "fresh", "revision": 0, "source": "external"}


def test_cli_edits_in_another_process_emit_external_events(live, three_songs_clip):
    root = three_songs_clip.parent
    server = live(root=root)
    server.http.post("/api/v1/compose/projects", json={"name": "show"})
    # An editor that has the video open: it is in the server's project registry, so it is watched.
    server.http.post("/api/v1/open", json={"path": three_songs_clip.name})
    stream = server.stream(params={"kind": "project"})

    _cli("compose", "add-track", "show", "--kind", "audio", "--root", root)
    event = stream.next(10)
    assert event["data"] == {"kind": "timeline", "project_id": "show", "revision": 1, "source": "external"}
    assert server.http.get("/api/v1/compose/projects/show").json()["revision"] == 1

    _cli("add-split", three_songs_clip, "--at", "6")
    event = stream.next(10)
    assert event["data"] == {
        "kind": "split",
        "project_id": three_songs_clip.name,
        "revision": 1,
        "source": "external",
    }
    assert server.http.get("/api/v1/state", params={"path": three_songs_clip.name}).json()["revision"] == 1


def test_a_project_opened_while_streaming_is_not_reported_as_changed(live, three_songs_clip):
    root = three_songs_clip.parent
    server = live(root=root)
    LocalProjectStore(root).save_split(
        three_songs_clip.name, [__import__("split_video.store", fromlist=["SavedSegment"]).SavedSegment(0.0, 5.0)]
    )
    stream = server.stream(params={"kind": "project"})
    server.http.post("/api/v1/open", json={"path": three_songs_clip.name})  # existing revision 1
    assert stream.quiet_for(0.5) == []


# --- filters ----------------------------------------------------------------------------------------


def test_filters_narrow_the_stream(live):
    server = live()
    only_jobs = server.stream(params={"kind": "job"})
    only_projects = server.stream(params=[("kind", "project")])
    comma = server.stream(params={"kind": "job,project"})
    one_job_id = {}

    job_a = server.jobs.record_done("export", label="a")
    one = server.stream(params={"job": job_a})  # a job that already finished: stays open, sees only its own
    server.http.post("/api/v1/compose/projects", json={"name": "p1"})
    server.http.post("/api/v1/compose/projects", json={"name": "p2"})
    job_b = server.jobs.record_done("export", label="b")
    one_job_id["b"] = job_b
    by_project = server.stream(params={"project": "p2"})
    server.http.post("/api/v1/compose/projects/p2/tracks", json={"kind": "audio"})
    server.http.post("/api/v1/compose/projects/p1/tracks", json={"kind": "audio"})

    jobs_seen = only_jobs.quiet_for(0.5)
    assert jobs_seen and {f["event"] for f in jobs_seen} == {"job"}
    projects_seen = only_projects.quiet_for(0.5)
    assert len(projects_seen) == 4 and {f["event"] for f in projects_seen} == {"project"}
    both = comma.quiet_for(0.5)
    assert {f["event"] for f in both} == {"job", "project"}
    mine = one.quiet_for(0.3)
    assert mine == []  # job a's events happened before this stream opened
    assert [f["data"]["project_id"] for f in by_project.quiet_for(0.5)] == ["p2"]

    follow = server.stream(params={"job": job_b})
    again = server.jobs.submit("export", lambda ctx: None)
    assert follow.quiet_for(0.4) == []  # another job's events don't leak in
    assert server.jobs.wait(again, 5).finished
    watch = server.stream(params={"job": again}, wait_hello=False)
    hello = watch.next(5)
    assert hello["event"] == "hello" and hello["data"]["jobs"] == []


def test_job_filter_follows_one_job_and_stays_open(live):
    server = live()
    go = threading.Event()
    job_id = server.jobs.submit("export", lambda ctx: go.wait(5) and ctx.progress(1, 1), total=1)
    other = server.jobs.submit("export", lambda ctx: go.wait(5), total=1)
    stream = server.stream(params={"job": job_id}, wait_hello=False)
    hello = stream.next(5)
    assert [j["id"] for j in hello["data"]["jobs"]] == [job_id]  # catch-up lists only that job
    go.set()
    frames = stream.until(lambda f: f["data"]["job"]["state"] == "done" and f["data"]["type"] == "state", name="job")
    assert {f["data"]["job"]["id"] for f in frames} == {job_id}
    assert server.jobs.wait(other, 5).finished
    assert not stream.ended  # finishing the job does not end the stream


def test_an_unknown_kind_is_a_400(live):
    server = live()
    response = server.http.get("/api/v1/events", params={"kind": "nope"})
    assert response.status_code == 400 and "unknown kind" in response.json()["detail"]


def test_hello_catches_a_late_subscriber_up_on_active_jobs(live):
    server = live()
    release = threading.Event()
    running = server.jobs.submit("render", lambda ctx: release.wait(5), label="r", total=5)
    _wait_for(lambda: server.jobs.status(running).state == "running")
    stream = server.stream(wait_hello=False)
    hello = stream.next(5)
    assert hello["event"] == "hello" and "id" not in hello and hello["retry"] == "2000"
    assert hello["data"]["resync"] is False and abs(hello["data"]["time"] - time.time()) < 5
    assert [(j["id"], j["state"]) for j in hello["data"]["jobs"]] == [(running, "running")]
    release.set()


# --- resume, keepalive, overflow, cleanup, shutdown --------------------------------------------------


def test_last_event_id_replays_what_a_reconnecting_client_missed(live):
    server = live(EventsConfig(poll_interval=5, keepalive_interval=5, progress_interval=0, idle_linger=30))
    first = server.stream(params={"kind": "project"})
    server.http.post("/api/v1/compose/projects", json={"name": "r"})
    seen = first.next(5)
    first.close()
    last_id = seen["id"]
    _wait_for(lambda: server.bus.client_count == 0)
    server.http.post("/api/v1/compose/projects/r/tracks", json={"kind": "audio"})  # while disconnected
    server.http.post("/api/v1/compose/projects/r/tracks", json={"kind": "audio"})
    again = server.stream(params={"kind": "project"}, headers={"Last-Event-ID": last_id}, wait_hello=False)
    hello = again.next(5)
    assert hello["data"]["resumed"] is True and hello["data"]["resync"] is False
    replay = [again.next(5), again.next(5)]
    assert [f["data"]["revision"] for f in replay] == [1, 2]
    assert [int(f["id"]) for f in replay] == [int(last_id) + 1, int(last_id) + 2]
    assert again.quiet_for(0.3) == []


def test_a_last_event_id_that_cannot_be_replayed_asks_for_a_resync(live):
    server = live(EventsConfig(poll_interval=5, keepalive_interval=5, progress_interval=0, ring_size=3, idle_linger=30))
    server.http.post("/api/v1/compose/projects", json={"name": "r"})
    first = server.stream(params={"kind": "project"})
    for _ in range(8):
        server.http.post("/api/v1/compose/projects/r/tracks", json={"kind": "audio"})
    assert len(first.quiet_for(0.5)) == 8
    first.close()
    old = server.stream(headers={"Last-Event-ID": "1"}, wait_hello=False).next(5)
    assert old["data"]["resync"] is True and old["data"]["resumed"] is False
    future = server.stream(headers={"Last-Event-ID": "99999"}, wait_hello=False).next(5)
    assert future["data"]["resync"] is True
    junk = server.stream(headers={"Last-Event-ID": "abc"}, wait_hello=False).next(5)
    assert junk["data"]["resync"] is False and junk["data"]["resumed"] is False


def test_keepalive_comments_arrive_while_quiet(live):
    server = live(EventsConfig(poll_interval=5, keepalive_interval=0.1))
    stream = server.stream()
    assert stream.next(5, keepalives=True) == {"comment": "keepalive"}
    assert stream.next(5, keepalives=True) == {"comment": "keepalive"}


def test_response_headers(live):
    server = live()
    stream = server.stream()
    headers = stream.response.headers
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-store" and headers["x-accel-buffering"] == "no"
    assert stream.response.status_code == 200


def test_a_client_that_falls_behind_is_dropped_with_overflow():
    async def scenario():
        jobs = InProcessJobQueue(synchronous=True)
        bus = EventBus(jobs, InMemoryProjectStore(), list, EventsConfig(client_queue_size=3, progress_interval=0))
        slow = await bus.connect(frozenset({"job", "project"}), None, None, None)
        fast = await bus.connect(frozenset({"job", "project"}), None, None, None)
        stream = bus.stream(fast)
        got: list[str] = []

        async def drain():
            async for frame in stream:
                got.append(frame)

        drainer = asyncio.create_task(drain())
        for _ in range(5):
            jobs.record_done("export")  # 2 events each, the slow client never reads
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert bus.client_count == 1  # the slow one was dropped, the producer never blocked
        items = []
        while not slow.queue.empty():
            items.append(slow.queue.get_nowait())
        assert len(items) == 2 and items[0].startswith("event: overflow")
        assert sum(f.startswith("id:") for f in got) == 10  # the fast one saw everything
        bus.close()
        await asyncio.wait_for(drainer, 2)
        assert got[-1].startswith("event: shutdown")

    asyncio.run(scenario())


def test_listeners_and_the_watcher_are_cleaned_up_after_a_disconnect(live):
    from split_video import store as store_module

    server = live()
    assert not server.bus.listening and not server.bus.watcher_running  # idle bus: nothing registered
    assert not any(t.name == "events-watcher" for t in threading.enumerate())
    assert server.jobs._listeners == []

    a, b = server.stream(), server.stream()
    assert server.bus.client_count == 2 and server.bus.listening and server.bus.watcher_running
    assert len(server.jobs._listeners) == 1  # one subscription per app, not per client
    assert sum(fn == server.bus._on_revision for fn in store_module._listeners) == 1
    a.close()
    _wait_for(lambda: server.bus.client_count == 1)
    assert server.bus.watcher_running  # one client left
    b.close()
    _wait_for(lambda: server.bus.client_count == 0)
    _wait_for(lambda: not server.bus.watcher_running)  # the poller stops at once
    _wait_for(lambda: not server.bus.listening)  # listeners go after the short linger
    assert server.jobs._listeners == []
    assert server.bus._on_revision not in store_module._listeners
    _wait_for(lambda: not any(t.name == "events-watcher" for t in threading.enumerate()))


def test_an_idle_server_never_polls_the_store(tmp_path):
    calls = []

    class CountingStore(LocalProjectStore):
        def peek_revisions(self, split_ids=()):
            calls.append(1)
            return super().peek_revisions(split_ids)

    app = create_app(tmp_path, DEFAULTS, store=CountingStore(tmp_path), events=FAST)
    from fastapi.testclient import TestClient

    client = TestClient(app)
    client.post("/api/v1/compose/projects", json={"name": "x"})
    client.post("/api/v1/compose/projects/x/tracks", json={"kind": "audio"})
    time.sleep(0.3)
    assert calls == [] and not app.state.events.watcher_running and not app.state.events.listening


def test_shutdown_ends_open_streams_promptly(live):
    server = live(EventsConfig(poll_interval=1.0, keepalive_interval=15.0, idle_linger=60))
    streams = [server.stream(), server.stream(params={"kind": "project"})]
    took = server.stop()
    assert took < 3.0, f"shutdown took {took:.2f}s with streams open"
    for stream in streams:
        last = None
        while (frame := stream.next(5)) is not None:
            last = frame
        assert last is not None and last["event"] == "shutdown"
    assert server.bus.client_count == 0 and not server.bus.watcher_running and not server.bus.listening
    assert server.jobs._listeners == []


def test_polling_endpoints_are_unchanged_while_streaming(live, three_songs_clip):
    server = live(root=three_songs_clip.parent)
    server.stream()
    job_id = server.http.post(
        "/api/v1/export", params={"path": three_songs_clip.name}, json={"segments": [{"start": 0.0, "end": 4.0}]}
    ).json()["job_id"]
    _wait_for(lambda: server.http.get(f"/api/v1/export/{job_id}").json()["status"] == "done", 30)
    assert server.http.get(f"/api/v1/jobs/{job_id}").json()["state"] == "done"
    assert server.http.get("/api/state", params={"path": three_songs_clip.name}).status_code == 200


def test_the_events_route_is_documented_in_the_schema(tmp_path):
    op = create_app(tmp_path, DEFAULTS).openapi()["paths"]["/api/v1/events"]["get"]
    assert "text/event-stream" in op["responses"]["200"]["content"]
    assert "Last-Event-ID" in op["description"] and "external" in op["description"]
