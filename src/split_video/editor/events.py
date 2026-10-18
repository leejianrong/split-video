"""Server-sent events: job progress and project changes on one stream, `GET /api/v1/events`.

Wire format (`text/event-stream`, UTF-8; every `data:` is one line of JSON):

    retry: 2000
    event: hello
    data: {"time": 1760000000.1, "resync": false, "resumed": false, "last_event_id": 41,
           "jobs": [<job>, ...]}

    id: 42
    event: job
    data: {"type": "submitted" | "progress" | "state", "job": <job>, "previous": "queued"}

    id: 43
    event: project
    data: {"kind": "split" | "timeline", "project_id": "...", "revision": 7,
           "source": "local" | "external"}

    : keepalive

    event: overflow       (this client fell too far behind; the stream ends, reconnect)
    event: shutdown       (the server is stopping; the stream ends)

* `<job>` is exactly the `JobOut` shape of `GET /api/v1/jobs/{id}`. `previous` appears only on
  `state` events. A `progress` event also covers a message, partial result or cancel request.
* `hello` is sent first and has no `id`. `jobs` lists the jobs that are queued or running right
  now, so a late subscriber can catch up (it is omitted-as-empty when the filters exclude jobs).
* `project` events carry the project's new revision. `source` is `local` for a write made inside
  this server process, and `external` for one noticed by the watcher: the CLI and MCP server edit
  the same files from other processes, which `store.announce_revision` cannot announce. An external
  event may skip revisions (two edits within one poll interval give one event for the later one).
* `id`s are increasing integers shared by all clients. A reconnecting `EventSource` sends
  `Last-Event-ID`; if the server still holds every event after that id (the last `ring_size`),
  they are replayed after a `hello` with `"resumed": true`. Otherwise `hello` says
  `"resync": true` and nothing is replayed: refetch whatever you display from the polling endpoints.
* `progress` events are throttled to about `1 / progress_interval` per job per second. A progress
  dropped by the throttle is delivered late rather than lost, and always before the next `state`
  event, so the last progress of a job is never skipped. `submitted` and `state` are never dropped.
* Filters (`?kind=job|project`, repeatable or comma-separated; `?job=<id>`; `?project=<id>`)
  narrow what is sent. `hello`, `overflow` and `shutdown` are always sent.
* A `: keepalive` comment goes out whenever the stream has been quiet for `keepalive_interval`.

Design: one `EventBus` per app. Its listeners on the job queue and the revision hook, and its
watcher thread, exist only while a client is connected (plus `idle_linger` seconds afterwards, so a
single client's reconnect with `Last-Event-ID` can resume; the watcher itself stops at once).
With nobody connected the bus costs nothing: no thread, no polling, no listeners.

Listeners run on whatever thread produced the event and must not block, so they only hand the
event to the asyncio loop with `call_soon_threadsafe`; everything else (ids, ring buffer, throttle,
dedupe, client queues) runs on that loop and needs no locks. Each client has a bounded queue; a
client that overflows it is sent `overflow` and dropped, never waited for.

The watcher thread polls `ProjectStore.peek_revisions` every `poll_interval` seconds (timeline
projects and the split projects clients have opened), and the loop emits an `external` event for
each revision above the last one emitted for that project. In-process writes set that same
"last emitted" mark, so one write gives exactly one event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from starlette.responses import StreamingResponse

from split_video.editor.job_routes import job_out
from split_video.jobs import JobEvent, JobProgress, JobQueue, JobStateChanged
from split_video.store import ProjectStore, RevisionChange, subscribe_revisions

log = logging.getLogger(__name__)

KINDS = ("job", "project")
_CLOSE = object()  # queue sentinel: end the stream


@dataclass(frozen=True)
class EventsConfig:
    """Tunables for the event stream; tests shrink the intervals."""

    poll_interval: float = 1.0  # watcher: seconds between revision polls while a client is connected
    keepalive_interval: float = 15.0  # seconds of silence before a `: keepalive` comment
    ring_size: int = 500  # events kept for Last-Event-ID resume
    client_queue_size: int = 1000  # per-client backlog before it is dropped with `overflow`
    progress_interval: float = 0.1  # minimum seconds between progress events of one job
    idle_linger: float = 60.0  # keep listening this long after the last client left (for resume)
    confirm_grace: float = 0.05  # watcher: wait this long before calling a change external


@dataclass(frozen=True)
class _Event:
    id: int
    name: str  # "job" | "project"
    data: dict[str, Any]
    job_id: str | None = None
    project_id: str | None = None

    @property
    def frame(self) -> str:
        return f"id: {self.id}\nevent: {self.name}\ndata: {json.dumps(self.data, separators=(',', ':'))}\n\n"


def _frame(name: str, data: dict[str, Any] | None = None, *, retry: int | None = None) -> str:
    head = f"retry: {retry}\n" if retry is not None else ""
    return f"{head}event: {name}\ndata: {json.dumps(data or {}, separators=(',', ':'))}\n\n"


@dataclass
class _Throttle:
    last_sent: float = float("-inf")
    pending: JobProgress | None = None
    timer: asyncio.TimerHandle | None = None


class _Client:
    def __init__(self, kinds: frozenset[str], job: str | None, project: str | None, queue_size: int) -> None:
        self.kinds = kinds
        self.job = job
        self.project = project
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_size)
        self.closed = False

    def accepts(self, event: _Event) -> bool:
        if event.name not in self.kinds:
            return False
        if event.name == "job":
            return self.job is None or event.job_id == self.job
        return self.project is None or event.project_id == self.project

    def close_with(self, frame: str | None) -> None:
        """End this client's stream, after `frame` if given, making room if its queue is full."""
        if self.closed:
            return
        self.closed = True
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if frame is not None:
            self.queue.put_nowait(frame)
        self.queue.put_nowait(_CLOSE)


def parse_filters(
    kind: list[str], job: str | None, project: str | None
) -> tuple[frozenset[str], str | None, str | None]:
    """The effective (kinds, job id, project id) of a request; `ValueError` for an unknown kind.

    With no `kind`, `job=` alone means job events only and `project=` alone project events only."""
    wanted = [k.strip() for value in kind for k in value.split(",") if k.strip()]
    unknown = [k for k in wanted if k not in KINDS]
    if unknown:
        raise ValueError(f"unknown kind {unknown[0]!r}; use job or project")
    if wanted:
        kinds = frozenset(wanted)
    elif job or project:
        kinds = frozenset(k for k, f in (("job", job), ("project", project)) if f)
    else:
        kinds = frozenset(KINDS)
    return kinds, job or None, project or None


class EventBus:
    """Fans job events and project revision changes out to the connected SSE clients."""

    def __init__(
        self,
        jobs: JobQueue,
        store: ProjectStore,
        split_ids: Callable[[], list[str]],
        config: EventsConfig | None = None,
    ) -> None:
        self.config = config or EventsConfig()
        self._jobs = jobs
        self._store = store
        self._split_ids = split_ids  # the split projects to watch: those clients have opened
        # Everything below is touched on the loop thread only, except where noted.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._attach_lock: asyncio.Lock | None = None
        self._clients: set[_Client] = set()
        self._next_id = 1
        self._ring: deque[_Event] = deque(maxlen=max(1, self.config.ring_size))
        self._last: dict[tuple[str, str], int] = {}  # last revision emitted per project
        self._baselined = False  # a revision snapshot has been taken in this lifetime
        self._throttle: dict[str, _Throttle] = {}
        self._unsubscribers: list[Callable[[], None]] = []
        self._watcher: tuple[threading.Thread, threading.Event] | None = None
        self._linger: asyncio.TimerHandle | None = None
        self._confirming: list[asyncio.TimerHandle] = []
        self._closed = False

    # --- introspection (tests, diagnostics) -------------------------------------------------

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def listening(self) -> bool:
        """True while subscribed to the job queue and the revision hook."""
        return bool(self._unsubscribers)

    @property
    def watcher_running(self) -> bool:
        return self._watcher is not None and self._watcher[0].is_alive()

    # --- clients ------------------------------------------------------------------------------

    async def connect(
        self, kinds: frozenset[str], job: str | None, project: str | None, last_event_id: int | None
    ) -> _Client:
        """Register a client and queue its `hello` (and any replay). Starts listening if needed."""
        loop = asyncio.get_running_loop()
        client = _Client(kinds, job, project, self.config.client_queue_size)
        if self._closed:
            client.queue.put_nowait(_frame("shutdown"))
            client.queue.put_nowait(_CLOSE)
            client.closed = True
            return client
        if self._loop is not loop:
            self._loop, self._attach_lock = loop, asyncio.Lock()
        assert self._attach_lock is not None
        if self._linger is not None:
            self._linger.cancel()
            self._linger = None
        async with self._attach_lock:
            if not self._unsubscribers:
                self._unsubscribers = [self._jobs.subscribe(self._job_listener), subscribe_revisions(self._on_revision)]
            if not self.watcher_running:
                baseline = await asyncio.to_thread(self._peek)
                self._start_watcher()
                self._on_peek(baseline)
            if self._closed:
                client.queue.put_nowait(_frame("shutdown"))
                client.queue.put_nowait(_CLOSE)
                client.closed = True
                return client
            self._clients.add(client)  # hello, replay and registration happen with no await between
            self._greet(client, last_event_id)
        return client

    def _greet(self, client: _Client, last_event_id: int | None) -> None:
        latest = self._next_id - 1
        replay: list[_Event] = []
        resync = False
        if last_event_id is not None:
            first_kept = self._ring[0].id if self._ring else self._next_id
            if last_event_id == latest:
                pass  # nothing missed
            elif last_event_id < latest and last_event_id + 1 >= first_kept:
                replay = [e for e in self._ring if e.id > last_event_id and client.accepts(e)]
            else:
                resync = True
        jobs = []
        if "job" in client.kinds:
            active = [*self._jobs.list(state="running"), *self._jobs.list(state="queued")]
            jobs = [job_out(i).model_dump(mode="json") for i in active if client.job in (None, i.id)]
        hello = {
            "time": time.time(),
            "resync": resync,
            "resumed": last_event_id is not None and not resync,
            "last_event_id": latest,
            "jobs": jobs,
        }
        client.queue.put_nowait(_frame("hello", hello, retry=2000))
        for event in replay:
            client.queue.put_nowait(event.frame)

    async def stream(self, client: _Client) -> AsyncIterator[str]:
        """The response body for one client; ends on `shutdown`/`overflow` and always detaches."""
        try:
            while True:
                try:
                    item = await asyncio.wait_for(client.queue.get(), self.config.keepalive_interval)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is _CLOSE:
                    return
                yield item
        finally:
            self._detach(client)

    def _detach(self, client: _Client) -> None:
        client.closed = True
        if client not in self._clients:
            return
        self._clients.discard(client)
        if self._clients or self._closed:
            return
        self._stop_watcher()
        loop = self._loop
        if loop is None:
            return
        if self.config.idle_linger <= 0:
            self._expire()
        else:
            if self._linger is not None:
                self._linger.cancel()
            self._linger = loop.call_later(self.config.idle_linger, self._expire)

    def _expire(self) -> None:
        """Nobody has been connected for `idle_linger`: stop listening and forget everything."""
        self._linger = None
        if self._clients:
            return
        self._unsubscribe()
        self._ring.clear()
        self._last.clear()
        self._baselined = False
        for t in self._throttle.values():
            if t.timer is not None:
                t.timer.cancel()
        self._throttle.clear()
        self._next_id += 1  # a hole in the ids, so a stale Last-Event-ID is told to resync

    def _unsubscribe(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers = []

    def close(self) -> None:
        """Server shutdown: tell every client, end their streams, stop listening. Safe from any thread."""
        loop = self._loop
        if loop is None:
            self._closed = True
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._close_on_loop()
        elif not loop.is_closed():
            loop.call_soon_threadsafe(self._close_on_loop)
            self._closed = True
            self._unsubscribe()
            self._stop_watcher()

    def _close_on_loop(self) -> None:
        self._closed = True
        for client in list(self._clients):
            client.close_with(_frame("shutdown"))
        self._clients.clear()
        self._unsubscribe()
        self._stop_watcher()
        for handle in [self._linger, *self._confirming, *(t.timer for t in self._throttle.values())]:
            if handle is not None:
                handle.cancel()
        self._throttle.clear()

    # --- publishing (loop thread) --------------------------------------------------------------

    def _publish(
        self, name: str, data: dict[str, Any], *, job_id: str | None = None, project_id: str | None = None
    ) -> None:
        event = _Event(self._next_id, name, data, job_id, project_id)
        self._next_id += 1
        self._ring.append(event)
        for client in list(self._clients):
            if not client.accepts(event):
                continue
            try:
                client.queue.put_nowait(event.frame)
            except asyncio.QueueFull:
                log.warning("dropping an event-stream client that is too far behind")
                self._clients.discard(client)
                client.close_with(_frame("overflow", {"last_event_id": event.id}))

    def _handoff(self, fn: Callable[..., None], *args: Any) -> None:
        """Run `fn(*args)` on the loop from any thread; a no-op if there is no usable loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # the loop closed under us
            pass

    # --- jobs ----------------------------------------------------------------------------------

    def _job_listener(self, event: JobEvent) -> None:  # any thread, queue lock held: hand off only
        self._handoff(self._on_job, event)

    def _on_job(self, event: JobEvent) -> None:
        if self._closed:
            return
        job_id = event.job.id
        if isinstance(event, JobProgress):
            self._on_progress(event)
            return
        if isinstance(event, JobStateChanged):
            self._flush_progress(job_id)  # the last progress always precedes the state change
            if event.job.finished:
                self._throttle.pop(job_id, None)
            data: dict[str, Any] = {"type": "state", "job": _job_json(event), "previous": event.previous}
        else:
            data = {"type": "submitted", "job": _job_json(event)}
        self._publish("job", data, job_id=job_id)

    def _on_progress(self, event: JobProgress) -> None:
        job_id = event.job.id
        state = self._throttle.setdefault(job_id, _Throttle())
        wait = state.last_sent + self.config.progress_interval - time.monotonic()
        if wait <= 0 and state.pending is None:
            self._send_progress(state, event)
            return
        state.pending = event  # the newest wins; it goes out when the interval is up or before a state change
        if state.timer is None and self._loop is not None:
            state.timer = self._loop.call_later(max(wait, 0.0), self._flush_progress, job_id)

    def _flush_progress(self, job_id: str) -> None:
        state = self._throttle.get(job_id)
        if state is None:
            return
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        if state.pending is not None:
            pending, state.pending = state.pending, None
            self._send_progress(state, pending)

    def _send_progress(self, state: _Throttle, event: JobProgress) -> None:
        state.last_sent = time.monotonic()
        self._publish("job", {"type": "progress", "job": _job_json(event)}, job_id=event.job.id)

    # --- project revisions -----------------------------------------------------------------------

    def _on_revision(self, change: RevisionChange) -> None:  # the writer's thread: hand off only
        if change.origin is not None and change.origin is not self._store:
            return  # another store instance in this process: the watcher reports it as external
        self._handoff(self._on_local, change)

    def _on_local(self, change: RevisionChange) -> None:
        key = (change.kind, change.project_id)
        if self._closed or change.revision <= self._last.get(key, -1):
            return  # already reported (the watcher got there first)
        self._last[key] = change.revision
        self._emit_project(change.kind, change.project_id, change.revision, "local")

    def _emit_project(self, kind: str, project_id: str, revision: int, source: str) -> None:
        data = {"kind": kind, "project_id": project_id, "revision": revision, "source": source}
        self._publish("project", data, project_id=project_id)

    def _peek(self) -> dict[tuple[str, str], int]:
        return self._store.peek_revisions(self._split_ids())

    def _on_peek(self, snapshot: dict[tuple[str, str], int]) -> None:
        if self._closed:
            return
        changed: list[tuple[tuple[str, str], int]] = []
        for key, revision in snapshot.items():
            previous = self._last.get(key)
            if previous is None:
                if key[0] == "timeline" and self._baselined:
                    changed.append((key, revision))  # a timeline project created since the last look
                else:
                    self._last[key] = revision  # first sight of it: the starting point, not a change
            elif revision > previous:
                changed.append((key, revision))
        self._baselined = True
        if not changed:
            return
        if self.config.confirm_grace <= 0 or self._loop is None:
            self._confirm(changed)
            return
        # An in-process write may be on its way to `_on_local`; give it a moment so it is reported
        # once, as local, rather than also noticed here.
        handle: asyncio.TimerHandle | None = None

        def later() -> None:
            if handle is not None and handle in self._confirming:
                self._confirming.remove(handle)
            self._confirm(changed)

        handle = self._loop.call_later(self.config.confirm_grace, later)
        self._confirming.append(handle)

    def _confirm(self, changed: list[tuple[tuple[str, str], int]]) -> None:
        if self._closed:
            return
        for key, revision in changed:
            if revision > self._last.get(key, -1):
                self._last[key] = revision
                self._emit_project(key[0], key[1], revision, "external")

    # --- the watcher thread ------------------------------------------------------------------------

    def _start_watcher(self) -> None:
        stop = threading.Event()
        thread = threading.Thread(target=self._watch, args=(stop,), name="events-watcher", daemon=True)
        self._watcher = (thread, stop)
        thread.start()

    def _stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher[1].set()
            self._watcher = None

    def _watch(self, stop: threading.Event) -> None:
        while not stop.wait(self.config.poll_interval):
            try:
                snapshot = self._peek()
            except Exception:
                log.exception("project revision poll failed")
                continue
            if stop.is_set():
                return
            self._handoff(self._on_peek, snapshot)


class _EventStreamResponse(StreamingResponse):
    """A streaming response that detaches its client however it ends, even if the body never started
    (a disconnect before the first byte means the body generator's own `finally` never runs)."""

    def __init__(self, bus: EventBus, client: _Client) -> None:
        super().__init__(
            bus.stream(client),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
        self._bus = bus
        self._client = client

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._bus._detach(self._client)


def _job_json(event: JobEvent) -> dict[str, Any]:
    return job_out(event.job).model_dump(mode="json")


SSE_DESCRIPTION = (
    "A `text/event-stream` of job progress and project changes; keep it open instead of polling. "
    "Frames: `event: hello` first (server `time`, `resync`, `resumed`, `last_event_id`, and the `jobs` "
    "queued or running now); `event: job` with `{type: submitted|progress|state, job: <the JobOut of "
    "GET /jobs/{id}>, previous?: <state>}`; `event: project` with `{kind: split|timeline, project_id, "
    "revision, source: local|external}` (`local`: written through this server; `external`: another "
    "process, e.g. the CLI or MCP server, changed the project and the server noticed within about a "
    "second); `: keepalive` comment lines when quiet; `event: overflow` (the client fell behind, "
    "reconnect) and `event: shutdown` (the server is stopping) end the stream. Every `job` and "
    "`project` frame has an increasing `id:`; a reconnect with `Last-Event-ID` replays what it missed "
    "from the last 500 events, or gets `hello` with `resync: true` (refetch state) if that is no "
    "longer possible. `progress` is throttled to about 10 per second per job and the final progress "
    "always precedes the job's `state` event. `kind` (`job`, `project`; repeatable or comma-separated), "
    "`job` and `project` filter the stream; `hello`, `overflow` and `shutdown` are always sent. "
    "The polling endpoints stay available."
)


def make_router(bus: EventBus) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/events",
        summary="Event stream (SSE)",
        description=SSE_DESCRIPTION,
        response_class=StreamingResponse,
        responses={
            200: {"description": "The event stream.", "content": {"text/event-stream": {"schema": {"type": "string"}}}}
        },
    )
    async def events(
        kind: Annotated[
            list[str] | None,
            Query(description="Only these kinds of event: `job`, `project` (repeat or comma-separate)."),
        ] = None,
        job: Annotated[
            str | None, Query(description="Only this job's events (implies `kind=job` unless `kind` is given).")
        ] = None,
        project: Annotated[
            str | None,
            Query(
                description="Only this project's events (a project id; implies `kind=project` unless `kind` is given)."
            ),
        ] = None,
        last_event_id: Annotated[
            str | None, Header(alias="Last-Event-ID", description="Resume after this event id.")
        ] = None,
    ) -> StreamingResponse:
        try:
            kinds, job_id, project_id = parse_filters(kind or [], job, project)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        resume = (
            int(last_event_id)
            if last_event_id and last_event_id.strip().isascii() and last_event_id.strip().isdigit()
            else None
        )
        client = await bus.connect(kinds, job_id, project_id, resume)
        return _EventStreamResponse(bus, client)

    return router
