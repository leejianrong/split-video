# The new editor frontend (scaffold)

The editor is being rebuilt as one Svelte 5 app (decisions: [ADR 0004](adr/0004-frontend-stack-and-structure.md),
layout contract: [ux-spec.md](ux-spec.md)). Today it is a scaffold: the six regions of the shell as empty frames,
labelled as such, served at **`/app/`**. The old editor at `/` and `/compose.html` is unchanged.

## Docker

The image builds the app in a Node stage and copies only the result in, so there is nothing to install:

```bash
make dev        # then open http://localhost:8765/app/
```

## From a checkout

Node 24 (see `frontend/.nvmrc`) is needed for the frontend only.

```bash
make frontend        # npm ci && npm run build, into src/split_video/editor/static_app/
uv run split-video edit videos/   # /app/ now serves the build
```

Without a build, `/app/` answers 404 with a hint to run it, and the old pages work as before. `static_app/` is
gitignored; it is never committed.

## Dev loop

```bash
uv run split-video edit videos/       # API on 127.0.0.1:8765
make frontend-dev                     # Vite on http://localhost:5173/app/, HMR
```

Vite proxies `/api` and `/media` (including the `/api/v1/events` SSE stream) to port 8765, so the browser sees one
origin and FastAPI needs no CORS settings.

## Scripts (`frontend/`)

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server with the proxy |
| `npm run build` | Production build into `src/split_video/editor/static_app/` |
| `npm run check` | `svelte-check` with warnings as errors (includes a11y warnings) |
| `npm run lint` | ESLint and `prettier --check` |
| `npm run format` | `prettier --write` |
| `npm run test` | Vitest |
| `npm run api:generate` | Regenerate the typed API client from `../openapi.json` (also `make api-client`) |

CI runs the same set in the `frontend` job (plus the API client drift check below), then builds the wheel and sdist and runs
`scripts/check_wheel_contents.py` on them.

## API client

`frontend/src/lib/api/generated/` is generated from the committed `openapi.json` by
[`@hey-api/openapi-ts`](https://heyapi.dev) (exact-pinned, config in `frontend/openapi-ts.config.ts`): types, one SDK
function per operation and the fetch client. It is committed, so a route change shows up as a type diff in the same PR,
and it needs no Python to regenerate. After `make openapi schemas errors` run **`make api-client`** (or
`npm run api:generate` in `frontend/`); CI and the pre-push hook fail on a stale or untracked generated file.
The generated directory is excluded from eslint and prettier but is type-checked by `npm run check`.

Things to know about the output:

- Only `/api/v1` is in the schema (the deprecated unversioned aliases are not), so there is one function per
  operation. Names come from FastAPI's operation ids, so they are long (`whoamiApiV1WhoamiGet`,
  `listAssetsApiV1AssetsGet`); fixing that means setting `operation_id`s in the backend, not renaming in the frontend.
- The client throws on failure (`throwOnError: true`), and success returns `{ data, request, response }`.

`frontend/src/lib/api/client.ts` is the hand-written layer, and the only place components import the API from:

- `ApiError` (`code`, `message`, `fix`, `status`, `details`, `currentRevision`, `isRevisionConflict`): every failed call
  rejects with it, built from the error catalogue body ([errors.md](errors.md)). A body of another shape gives
  `code: "unknown"` and a message from the status; no response at all gives `code: "network"`, `status: 0`.
- `configureClient({ token, baseUrl })`: the optional bearer token (sent as `Authorization`) and base URL. The default
  is same origin, so it works behind FastAPI at `/app/` and behind Vite's proxy in dev.
- `ifMatch(revision)`: spread into a write's options to send `If-Match: "<revision>"` ([revisions.md](revisions.md)).
  A stale write rejects with `error.isRevisionConflict` and `error.currentRevision`.
- `connectEvents({ onEvent, onStatus, query })`: `GET /api/v1/events` with reconnect and exponential backoff (500 ms
  doubling to 15 s, reset when the stream opens). `onStatus` reports `connecting`, `open`, `reconnecting` and `closed`;
  an `open` with `reconnect: true` means refetch what you mirror. An `EventSource` can be injected for tests. Browsers
  cannot send the bearer token on an `EventSource` (see [api.md](api.md)).

The top bar's server status line (`lib/shell/ServerStatus.svelte`) calls `GET /api/v1/whoami` through this client.

## Caching

Vite content-hashes everything under `/app/assets/`, so those files are served `public, max-age=31536000,
immutable`. `/app/index.html` (which names the hashes) and the old pages stay `no-store`.

## Packaging

The wheel and sdist must contain `split_video/editor/static_app/`: pip users have no Node. `uv_build` packs
gitignored files under the module root, so running `npm ci && npm run build` in `frontend/` before `uv build` is all
that is needed. Check it with `uv build --out-dir dist && python3 scripts/check_wheel_contents.py dist`. The
repository has no job that publishes the wheel or sdist yet (a `v*.*.*` tag only publishes the Docker image); the
job that does must run the build first, as the `frontend` CI job does.
