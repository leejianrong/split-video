"""The identity seam over HTTP: local mode, the bearer token, job ownership, per-principal stores."""

import asyncio
import json
import logging
import socket
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from split_video.editor.app import create_app
from split_video.editor.auth import TOKEN_ENV, TokenAuth, bearer_token
from split_video.editor.events import EventsConfig
from split_video.editor.schemas import StateParams
from split_video.editor.serve import make_server
from split_video.ffmpeg import MediaInfo
from split_video.identity import LOCAL_PRINCIPAL, Principal
from split_video.jobs import InProcessJobQueue
from split_video.media_store import InMemoryMediaStore
from split_video.store import InMemoryProjectStore

DEFAULTS = StateParams(silence_threshold=-35.0, min_silence_duration=2.0, min_song_length=2.0, padding=0.15)
TOKEN = "fake-test-token-not-a-secret"  # an obviously fake value
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FAST = EventsConfig(poll_interval=0.05, keepalive_interval=0.2, progress_interval=0.0, idle_linger=0.2)


@pytest.fixture
def secured(monkeypatch, tmp_path):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    return TestClient(create_app(tmp_path, DEFAULTS))


@pytest.fixture
def local(tmp_path):
    return TestClient(create_app(tmp_path, DEFAULTS))


# --- local mode ---------------------------------------------------------------------------------


def test_local_mode_serves_everything_as_the_local_principal(local):
    body = local.get("/api/v1/whoami").json()
    assert body == {"id": "local", "name": "Local user", "kind": "local", "auth_required": False}
    assert local.get("/api/session").status_code == 200  # a deprecated alias, no header


def test_local_mode_ignores_a_stray_authorization_header(local):
    for header in ("Bearer whatever", "Basic Zm9vOmJhcg==", "garbage", "Bearer "):
        response = local.get("/api/v1/whoami", headers={"Authorization": header})
        assert response.status_code == 200 and response.json()["kind"] == "local"


def test_blank_env_token_is_local_mode(monkeypatch, tmp_path):
    monkeypatch.setenv(TOKEN_ENV, "   ")
    client = TestClient(create_app(tmp_path, DEFAULTS))
    assert client.get("/api/v1/whoami").json()["auth_required"] is False


# --- with a token: every route ---------------------------------------------------------------------


def _filled(path):
    for name in ("job_id", "name", "track_id", "clip_id", "asset_id"):
        path = path.replace("{" + name + "}", "x")
    return path


def _all_protected_requests(app):
    """(method, path) of every operation in the schema, its deprecated alias, and the two /media aliases."""
    seen = set()
    for path, methods in app.openapi()["paths"].items():
        for method in methods:
            seen.add((method.upper(), _filled(path)))
            legacy = path.removeprefix("/api/v1")
            legacy = legacy if legacy.startswith("/media/") else "/api" + legacy
            seen.add((method.upper(), _filled(legacy)))  # absent for v1-only routes: still must not be public
    return sorted(seen)


def _send(client, method, path, headers=None):
    params = {"kind": "bogus"} if path.endswith("/events") else None  # a bad filter answers 400 instead of streaming
    return client.request(method, path, headers=headers, params=params)


def test_every_route_needs_the_token_when_one_is_configured(secured):
    requests = _all_protected_requests(secured.app)
    assert len(requests) > 60
    for method, path in requests:
        for headers in (
            None,
            {"Authorization": "Bearer wrong-token"},
            {"Authorization": f"Basic {TOKEN}"},
            {"Authorization": "Bearer "},
            {"Authorization": "Bearer"},
            {"Authorization": TOKEN},
        ):
            response = _send(secured, method, path, headers)
            assert response.status_code == 401, (method, path, headers)
            assert response.headers["www-authenticate"] == "Bearer"
            assert set(response.json()) == {"detail", "error"} and response.json()["error"] == "unauthorized"
        assert _send(secured, method, path, AUTH).status_code != 401, (method, path)


def test_the_two_media_aliases_and_unknown_api_paths_are_protected_too(secured):
    for path in ("/media/source", "/media/file", "/media/asset/x", "/api/nope", "/api/v1/nope"):
        assert secured.get(path).status_code == 401, path
        assert secured.get(path, headers=AUTH).status_code != 401, path


def test_a_route_added_later_is_protected_without_doing_anything(secured):
    """The check is middleware over the prefixes, not something each route opts into."""

    @secured.app.get("/api/v1/added-later")
    def added_later():
        return {"ok": True}

    @secured.app.get("/media/added-later")
    def media_added_later():
        return {"ok": True}

    for path in ("/api/v1/added-later", "/media/added-later"):
        assert secured.get(path).status_code == 401
        # (the static mount registered last shadows it, so with the token it is a 404, not a 401)
        assert secured.get(path, headers=AUTH).status_code != 401


def test_the_401_shape_and_whoami_with_a_valid_token(secured):
    response = secured.get("/api/v1/whoami")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "detail": "Missing or invalid bearer token. Send `Authorization: Bearer <token>`.",
        "error": "unauthorized",
    }
    me = secured.get("/api/v1/whoami", headers={"authorization": f"bearer  {TOKEN}  "}).json()
    assert me == {"id": "token", "name": "API token", "kind": "token", "auth_required": True}


def test_the_static_page_the_schema_and_docs_stay_public(secured):
    assert secured.get("/").status_code == 200
    schema = secured.get("/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "description": schema.json()["components"]["securitySchemes"]["bearerAuth"]["description"],
    }
    assert secured.get("/docs").status_code == 200


def test_the_schema_declares_bearer_auth_and_a_401_on_every_operation():
    from pathlib import Path

    schema = json.loads((Path(__file__).resolve().parents[2] / "openapi.json").read_text())
    assert schema["security"] == [{"bearerAuth": []}]
    assert "SPLIT_VIDEO_TOKEN" in schema["components"]["securitySchemes"]["bearerAuth"]["description"]
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            assert operation["responses"]["401"]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/UnauthorizedOut"
            }, (method, path)


def test_the_token_never_leaks_into_responses_logs_or_output(monkeypatch, tmp_path, caplog, capsys):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    caplog.set_level(logging.DEBUG)
    client = TestClient(create_app(tmp_path, DEFAULTS))
    wrong = {"Authorization": f"Bearer {TOKEN}x"}
    for headers in (None, wrong, AUTH):
        for path in ("/api/v1/whoami", "/api/v1/browse", "/api/v1/jobs", "/media/file?path=x"):
            response = client.get(path, headers=headers)
            assert TOKEN not in response.text
            assert TOKEN not in json.dumps(dict(response.headers))
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert TOKEN not in caplog.text


def test_a_valid_token_works_for_project_and_compose_flows(three_songs_clip, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    client = TestClient(create_app(three_songs_clip.parent, DEFAULTS), headers=AUTH)
    name = three_songs_clip.name
    state = client.post("/api/v1/open", json={"path": name}).json()
    saved = client.post(
        "/api/v1/project",
        params={"path": name},
        json={"segments": [{"start": 0, "end": 5}, {"start": 8, "end": 13}]},
        headers={"If-Match": str(state["revision"])},
    )
    assert saved.status_code == 200 and len(saved.json()["segments"]) == 2
    assert client.get("/api/v1/media/source", params={"path": name}).status_code == 200
    created = client.post("/api/v1/compose/projects", json={"name": "demo"})
    assert created.status_code == 200
    export = client.post(
        "/api/v1/export", params={"path": name}, json={"segments": [{"start": 0, "end": 4}, {"start": 8, "end": 12}]}
    ).json()
    job = _wait(client, export["job_id"])
    assert job["state"] == "done" and job["owner"] == "token"
    assert [j["id"] for j in client.get("/api/v1/jobs").json()["jobs"]] == [job["id"]]


def _wait(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


# --- job ownership ------------------------------------------------------------------------------------

A = Principal(id="alice", name="Alice", kind="token")
B = Principal(id="bob", name="Bob", kind="token")


class TwoUsers:
    required = True

    def authenticate(self, request):
        return {"tok-a": A, "tok-b": B}.get(bearer_token(request) or "")


def _as(token):
    return {"Authorization": f"Bearer {token}"}


def _slow_work(ctx):
    for _ in range(200):
        ctx.raise_if_cancelled()
        time.sleep(0.01)


def test_a_principal_cannot_see_or_cancel_anothers_jobs(tmp_path):
    jobs = InProcessJobQueue()
    client = TestClient(create_app(tmp_path, DEFAULTS, jobs=jobs, authenticator=TwoUsers()))
    a_job = jobs.submit("export", _slow_work, owner=A.id)
    b_job = jobs.submit("export", _slow_work, owner=B.id)

    seen_by_a = client.get("/api/v1/jobs", headers=_as("tok-a")).json()["jobs"]
    assert [j["id"] for j in seen_by_a] == [a_job] and seen_by_a[0]["owner"] == "alice"
    seen_by_b = client.get("/api/v1/jobs", headers=_as("tok-b")).json()["jobs"]
    assert [j["id"] for j in seen_by_b] == [b_job]

    unknown = client.get("/api/v1/jobs/nope", headers=_as("tok-b"))
    foreign = client.get(f"/api/v1/jobs/{a_job}", headers=_as("tok-b"))
    assert foreign.status_code == 404 and foreign.json() == unknown.json()
    assert client.get(f"/api/v1/jobs/{a_job}", headers=_as("tok-a")).status_code == 200

    assert client.post(f"/api/v1/jobs/{a_job}/cancel", headers=_as("tok-b")).status_code == 404
    assert client.post(f"/api/v1/export/{a_job}/cancel", headers=_as("tok-b")).status_code == 404
    assert client.get(f"/api/v1/export/{a_job}", headers=_as("tok-b")).status_code == 404
    assert not jobs.status(a_job).cancel_requested  # B's attempts did nothing

    cancelled = client.post(f"/api/v1/jobs/{a_job}/cancel", headers=_as("tok-a"))
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    jobs.cancel(b_job)
    jobs.shutdown()


def test_the_per_kind_status_routes_are_owner_scoped(tmp_path):
    jobs = InProcessJobQueue()
    client = TestClient(create_app(tmp_path, DEFAULTS, jobs=jobs, authenticator=TwoUsers()))
    kinds = {"export": "/api/v1/export/", "analysis": "/api/v1/analyze/", "render": "/api/v1/compose/render/"}
    for kind, prefix in kinds.items():
        job_id = jobs.submit(kind, lambda ctx: None, owner=A.id)
        jobs.wait(job_id, 5)
        assert client.get(prefix + job_id, headers=_as("tok-a")).status_code == 200, kind
        assert client.get(prefix + job_id, headers=_as("tok-b")).status_code == 404, kind
        assert client.post(prefix + job_id + "/cancel", headers=_as("tok-b")).status_code == 404, kind
    jobs.shutdown()


def test_jobs_started_through_the_api_record_the_caller_as_owner(three_songs_clip):
    app = create_app(three_songs_clip.parent, DEFAULTS, authenticator=TwoUsers())
    client = TestClient(app)
    body = {"segments": [{"start": 0, "end": 4}]}
    started = client.post("/api/v1/export", params={"path": three_songs_clip.name}, json=body, headers=_as("tok-a"))
    job_id = started.json()["job_id"]
    assert _wait_as(client, job_id, "tok-a")["owner"] == "alice"
    assert client.get(f"/api/v1/jobs/{job_id}", headers=_as("tok-b")).status_code == 404


def _wait_as(client, job_id, token, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}", headers=_as(token)).json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_local_jobs_are_owned_by_local_and_ownerless_jobs_stay_visible_locally(tmp_path):
    jobs = InProcessJobQueue(synchronous=True)
    client = TestClient(create_app(tmp_path, DEFAULTS, jobs=jobs))
    ownerless = jobs.submit("export", lambda ctx: None)
    mine = jobs.submit("export", lambda ctx: None, owner=LOCAL_PRINCIPAL.id)
    listed = {j["id"]: j["owner"] for j in client.get("/api/v1/jobs").json()["jobs"]}
    assert listed == {ownerless: None, mine: "local"}


# --- the live event stream -----------------------------------------------------------------------------


class LiveServer:
    def __init__(self, app):
        self.app = app
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.base = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.server = make_server(app, "127.0.0.1", self.sock.getsockname()[1])
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve(sockets=[self.sock])), daemon=True, name="auth-test-uvicorn"
        )
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.01)
        assert self.server.started

    def stop(self):
        self.server.should_exit = True
        self.thread.join(15)
        assert not self.thread.is_alive()
        self.sock.close()


def _frames(live, token, until, params=None, timeout=10.0):
    """Read events (with `token`) until `until(frames)` is true or the deadline; returns the frames."""
    headers = {} if token is None else _as(token)
    frames, current = [], {}
    deadline = time.monotonic() + timeout
    with httpx.stream(
        "GET", f"{live.base}/api/v1/events", headers=headers, params=params, timeout=httpx.Timeout(5.0)
    ) as response:
        if response.status_code != 200:
            return response
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                break
            if line.startswith("event:"):
                current["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                current["data"] = json.loads(line.split(":", 1)[1])
            elif line == "" and current:
                frames.append(current)
                current = {}
                if until(frames):
                    break
    return frames


@pytest.fixture
def live(tmp_path):
    jobs = InProcessJobQueue()
    server = LiveServer(create_app(tmp_path, DEFAULTS, jobs=jobs, events=FAST, authenticator=TwoUsers()))
    server.jobs = jobs
    yield server
    server.stop()
    jobs.shutdown()


def test_the_event_stream_answers_401_instead_of_hanging(live):
    for token in (None, "wrong"):
        response = _frames(live, token, lambda f: True)
        assert isinstance(response, httpx.Response) and response.status_code == 401


def test_sse_job_events_are_filtered_by_owner(live):
    results = {}

    def listen(name, token):
        results[name] = _frames(
            live,
            token,
            lambda f: (
                any(x["event"] == "job" and x["data"]["job"]["owner"] == "bob" for x in f) if name == "b" else False
            ),
            timeout=3.0,
        )

    a = threading.Thread(target=listen, args=("a", "tok-a"))
    b = threading.Thread(target=listen, args=("b", "tok-b"))
    a.start(), b.start()
    time.sleep(0.5)
    live.jobs.submit("export", lambda ctx: None, owner=A.id, label="alice-job")
    live.jobs.submit("export", lambda ctx: None, owner=B.id, label="bob-job")
    a.join(15), b.join(15)

    def labels(frames):
        return {f["data"]["job"]["label"] for f in frames if f["event"] == "job"}

    assert labels(results["a"]) == {"alice-job"}
    assert labels(results["b"]) == {"bob-job"}


def test_sse_hello_lists_only_the_callers_active_jobs(live):
    a_job = live.jobs.submit("export", _slow_work, owner=A.id)
    b_job = live.jobs.submit("export", _slow_work, owner=B.id)
    hello_a = _frames(live, "tok-a", lambda f: True)[0]["data"]["jobs"]
    hello_b = _frames(live, "tok-b", lambda f: True)[0]["data"]["jobs"]
    assert [j["id"] for j in hello_a] == [a_job] and [j["id"] for j in hello_b] == [b_job]
    live.jobs.cancel(a_job), live.jobs.cancel(b_job)


def test_a_token_user_gets_events_for_a_real_export_and_shutdown_is_prompt(three_songs_clip, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    jobs = InProcessJobQueue()
    server = LiveServer(create_app(three_songs_clip.parent, DEFAULTS, jobs=jobs, events=FAST))
    try:
        stream_done = threading.Event()
        frames = []

        def listen():
            with httpx.stream("GET", f"{server.base}/api/v1/events", headers=AUTH, timeout=httpx.Timeout(10.0)) as r:
                assert r.status_code == 200
                current = {}
                try:
                    for line in r.iter_lines():
                        if line.startswith("event:"):
                            current["event"] = line.split(":", 1)[1].strip()
                        elif line.startswith("data:"):
                            current["data"] = json.loads(line.split(":", 1)[1])
                        elif line == "" and current:
                            frames.append(current)
                            current = {}
                except httpx.HTTPError:
                    pass
            stream_done.set()

        threading.Thread(target=listen, daemon=True).start()
        time.sleep(0.5)
        with httpx.Client(base_url=server.base, headers=AUTH, timeout=10) as http:
            started = http.post(
                "/api/v1/export",
                params={"path": three_songs_clip.name},
                json={"segments": [{"start": 0, "end": 4}]},
            )
            assert started.status_code == 200
            job_id = started.json()["job_id"]
            deadline = time.time() + 20
            while time.time() < deadline and not any(
                f["event"] == "job" and f["data"]["job"]["id"] == job_id and f["data"]["job"]["state"] == "done"
                for f in frames
            ):
                time.sleep(0.05)
        done = [f for f in frames if f["event"] == "job" and f["data"]["job"]["id"] == job_id]
        assert done and done[-1]["data"]["job"]["state"] == "done" and done[-1]["data"]["job"]["owner"] == "token"
        began = time.monotonic()
        server.stop()  # with the stream still open
        assert time.monotonic() - began < 4
        assert stream_done.wait(5)
    finally:
        jobs.shutdown()


# --- the store provider seam -----------------------------------------------------------------------------


class PerPrincipalStores:
    """A different set of in-memory stores for every principal."""

    def __init__(self):
        self.projects: dict[str, InMemoryProjectStore] = {}
        self.media: dict[str, InMemoryMediaStore] = {}

    def project_store(self, principal):
        return self.projects.setdefault(principal.id, InMemoryProjectStore())

    def media_store(self, principal):
        if principal.id not in self.media:
            store = InMemoryMediaStore()
            store.add(f"{principal.id}-video.mp4", b"x" * 64, info=MediaInfo(30.0, 320, 240, True))
            self.media[principal.id] = store
        return self.media[principal.id]


def test_two_principals_see_different_projects_through_the_same_app(tmp_path):
    provider = PerPrincipalStores()
    client = TestClient(create_app(tmp_path, DEFAULTS, provider=provider, authenticator=TwoUsers()))

    def names(token):
        return [e["name"] for e in client.get("/api/v1/browse", headers=_as(token)).json()["entries"]]

    assert names("tok-a") == ["alice-video.mp4"] and names("tok-b") == ["bob-video.mp4"]
    assert client.get("/api/v1/state", params={"path": "alice-video.mp4"}, headers=_as("tok-b")).status_code == 404

    segments = {"segments": [{"start": 0, "end": 10}, {"start": 10, "end": 30}]}
    saved = client.post("/api/v1/project", params={"path": "alice-video.mp4"}, json=segments, headers=_as("tok-a"))
    assert saved.status_code == 200 and saved.json()["resumed"] is True
    assert client.get("/api/v1/state", params={"path": "alice-video.mp4"}, headers=_as("tok-a")).json()["resumed"]

    client.post("/api/v1/compose/projects", json={"name": "alice-cut"}, headers=_as("tok-a"))
    assert client.get("/api/v1/compose/projects", headers=_as("tok-a")).json() == {"projects": ["alice-cut"]}
    assert client.get("/api/v1/compose/projects", headers=_as("tok-b")).json() == {"projects": []}


def test_a_provider_and_loose_stores_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError):
        create_app(tmp_path, DEFAULTS, provider=PerPrincipalStores(), store=InMemoryProjectStore())


def test_wrapping_loose_stores_is_the_backward_compatible_path(tmp_path):
    store = InMemoryProjectStore()
    app = create_app(tmp_path, DEFAULTS, store=store, media=InMemoryMediaStore())
    assert app.state.stores.project_store(LOCAL_PRINCIPAL) is store
    assert app.state.stores.project_store(A) is store


def test_an_authenticator_can_be_injected_without_the_env(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    client = TestClient(create_app(tmp_path, DEFAULTS, authenticator=TokenAuth.single(TOKEN)))
    assert client.get("/api/v1/whoami").status_code == 401
    assert client.get("/api/v1/whoami", headers=AUTH).json()["auth_required"] is True
