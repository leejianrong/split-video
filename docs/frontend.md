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

CI runs the same set in the `frontend` job, then builds the wheel and sdist and runs
`scripts/check_wheel_contents.py` on them.

## Caching

Vite content-hashes everything under `/app/assets/`, so those files are served `public, max-age=31536000,
immutable`. `/app/index.html` (which names the hashes) and the old pages stay `no-store`.

## Packaging

The wheel and sdist must contain `split_video/editor/static_app/`: pip users have no Node. `uv_build` packs
gitignored files under the module root, so running `npm ci && npm run build` in `frontend/` before `uv build` is all
that is needed. Check it with `uv build --out-dir dist && python3 scripts/check_wheel_contents.py dist`. The
repository has no job that publishes the wheel or sdist yet (a `v*.*.*` tag only publishes the Docker image); the
job that does must run the build first, as the `frontend` CI job does.
