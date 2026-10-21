# split-video

Python CLI, MCP server and local web editor (FastAPI) for long recordings: split by silence, find highlights, compose and render with ffmpeg. Agent-first, local-first now, hosted later. Map: `src/split_video/` (engine, CLI, MCP), `src/split_video/editor/` (HTTP routes), `tests/unit`, `tests/integration`, `docs/` (user docs, `docs/adr/` decisions). Read `docs/adr/0002-agent-first-hosted-ready.md` before architectural work. If a doc and the code disagree, trust the code and fix the doc.

## Workflow

- **`main` is protected: PR-only, with `test` and `lint` CI checks required before merge.** Never push to `main`; work on a feature branch and open a PR.
- **Branch names are `<area>/<slug>`** (`editor/persist-analysis-cache`, `ci/dependabot`, `docs/readme-revamp`).
- **Anything persisted across `edit` runs lives in a sidecar file next to the video, never under `~/.cache/` or another central path.** Why: `make dev` runs the editor in a `--rm` Docker container with only the video directory mounted, so anything written elsewhere is silently wiped on the next run.
- Docs: the README leads with the Docker image and stays short; per-feature detail goes in `docs/`. Update docs in the same PR as the behaviour they describe.

## Checks

- Cheap gate (CI `lint`, mirrored by `.githooks/pre-push`; seconds): `uv run ruff check . && uv run ruff format --check .` plus the four `scripts/export_*.py --check` drift checks.
- Tests (CI `test`): `uv run pytest tests/unit` takes about 3 minutes, the full `uv run pytest` 5-10 (needs `ffmpeg`). Run both in the background and wait; do not poll.
- ffmpeg and server tests need hard timeouts (SPL-110: a wedged ffmpeg held CI for 6 h). pytest has a 300 s per-test limit; spawn ffmpeg with `-nostdin`, never read an SSE stream without a deadline, and start servers in the background and kill them by PID.

## Architecture rules (M1 seams)

New work goes through the seams, not around them:

- **`ProjectStore` and `MediaStore`** (local and in-memory implementations) own every path and file; the engine never builds paths. Media locations are opaque ids. Slow work runs as a job on the job queue (real cancel via `run_cancellable`). Writes carry revisions (`If-Match` / `--base-revision` / MCP `base_revision`).
- **The engine holds the rules; HTTP routes, CLI and MCP tools are thin adapters.** A capability that is "API, CLI and MCP" must exist on all three with the same output shape.
- **A new engine or adapter module goes into the ENGINE / ADAPTERS / GUARDED lists in `tests/unit/test_store_boundary.py`** (it fails on paths built outside the stores).
- **A new `/api/v1` route with no deprecated unversioned alias goes into `NO_LEGACY_ALIAS` in `tests/integration/test_api_versioning.py`.**
- **After changing routes, schemas or error codes, regenerate the committed files: `make openapi schemas errors`.** CI fails on drift.
- Public formats (`*.split-video-project.json`, `*.split-video-timeline.json`, `.split-video-assets.json`) change only with a version bump and a migration.

## Agent rules

- **Never run more than 2 subagents in parallel on this project**; split bigger work into sequential batches of at most 2.
- **Parallel agents each get an isolated worktree via `treehouse`** (`treehouse get --lease` to acquire, `treehouse return <path>` when done), not ad hoc `git worktree`, so they cannot edit the same files in the main tree. The gitignored YAMNet model lives only in the main checkout.
