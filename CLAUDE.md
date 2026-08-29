# split-video

A CLI (and, since the visual editor, a small local web app) that splits a long recording of back-to-back songs into individual files by detecting silence gaps with ffmpeg.

## Workflow

- **`main` is protected: PR-only, with `test` and `lint` CI checks required before merge.** Never push directly to `main` — work on a feature branch and open a PR.
- **Branch names follow `<area>/<slug>`** (e.g. `editor/persist-analysis-cache`, `ci/dependabot`, `tooling/ruff`).
- **Anything this app needs to persist across `edit` invocations must live in a sidecar file next to the video being edited, not a centralized path like `~/.cache/`.** `make dev` runs the editor in a `--rm`'d Docker container with only the video directory bind-mounted, so anything written outside that mount is silently wiped on the next run.

## Agent rules

- **Never run more than 2 subagents in parallel on this project.** If a task seems to want more, split it into sequential batches of at most 2.
- **When agents need to work in parallel on this repo, give each one an isolated git worktree via `treehouse`** (`treehouse get --lease` to acquire one, `treehouse return <path>` to release it when the agent is done) instead of ad hoc `git worktree` commands, so parallel agents can't collide by editing the same files in the main working tree.
