# HTTP API and API token

The editor's JSON API is versioned under `/api/v1` (media at
`/api/v1/media/source?path=...`). Its OpenAPI schema is committed as
[`openapi.json`](../openapi.json) at the repo root, and CI fails if it drifts
from the code. After changing a route or schema, regenerate it with
`make openapi`. The old unversioned paths (`/api/...`, `/media/source`,
`/media/file`) still work for one release as deprecated aliases (they send a
`Deprecation: true` header and are not in the schema).

Project state responses carry a `revision`, and writes take an optional
`If-Match` header; see [Revisions](revisions.md).

`GET /api/v1/events` is a server-sent-events stream of job progress and project
revision changes, including edits made by the CLI or MCP server in another
process (`curl -N localhost:8765/api/v1/events`). The polling endpoints keep
working; the frame format is in the route's description in the schema.

## API token (agents and hosted use)

By default the server is a local single-user tool and the API is open. Start
it with `SPLIT_VIDEO_TOKEN` set and every request under `/api/` and `/media/`
(including the event stream and the deprecated aliases) must carry
`Authorization: Bearer <token>`, or it gets a `401` with a `WWW-Authenticate:
Bearer` header and the body `{"detail": "...", "error": "unauthorized"}`. The
static page, `/openapi.json` and the docs stay public. `GET /api/v1/whoami`
returns the caller (`id`, `name`, `kind`, `auth_required`), which is an easy
way for an agent to check its token works:

```bash
SPLIT_VIDEO_TOKEN=choose-a-long-random-value split-video edit ~/recordings
curl -H "Authorization: Bearer $SPLIT_VIDEO_TOKEN" localhost:8765/api/v1/whoami
```

An unset or blank value means no checking, and the CLI and the MCP server
(local processes) never need a token. The server prints a line at startup
when token authentication is on, never the token. With a token, each caller
only sees its own jobs (the job's `owner`).

**Known limitation:** browsers cannot attach an `Authorization` header to
`<video src>` or `EventSource`, and the bundled web UI sends no token, so with
a token configured the bundled UI cannot use the API (the page loads, its API
calls get `401`). Token mode is for agents, API clients and hosted
deployments; UI support (for example a session cookie) comes with the
frontend rebuild. Project (revision) events on the event stream do not carry
an owner yet and go to every authenticated client.

## Errors

Every failure, on the CLI, the MCP server and the HTTP API, carries a stable `code`, a `message` and a `fix` that names
the next action (`split-video --json-errors ...` prints it as JSON). The codes are listed in [errors.md](errors.md).
