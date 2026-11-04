# Contributing

Local setup (`uv sync`, `make fetch-model`) is in
[docs/cli.md](docs/cli.md#run-without-docker-uv); building the image and `make dev`
are in [docs/docker.md](docs/docker.md#build-the-image-yourself). This file covers
the contributor workflow: running tests, the pre-push hook, and cutting a release.

## Tests

```bash
uv run pytest
```

The full run takes about 10 minutes and needs `ffmpeg`; `uv run pytest tests/unit`
is the quick layer. A few tests exercise real YAMNet inference and skip
automatically if you haven't run `make fetch-model`. Set
`SPLIT_VIDEO_LONG_TESTS=1` to also run the 3-hour classification check.

## Frontend

The Svelte app lives in `frontend/` and needs Node 24 (`frontend/.nvmrc`); see
[docs/frontend.md](docs/frontend.md). Before a PR that touches it run `npm run lint`, `npm run check`,
`npm run test` and `npm run build` there (CI's `frontend` job does the same); a change to what the editor does in the browser also runs `npm run e2e` ([docs/e2e.md](docs/e2e.md), CI's `e2e` job). A change to routes or schemas also
needs `make openapi schemas errors` followed by `make api-client` (`npm run api:generate`), and the regenerated
`frontend/src/lib/api/generated/` goes in the same PR; CI fails when it is stale.

## Pre-push hook

A git hook mirrors the cheap parts of CI before every push: `ruff check`,
`ruff format --check`, the generated-file drift checks (`openapi.json`, the
two schemas in `docs/schemas/`, `docs/errors.md`; fix with
`make openapi schemas errors`), when `frontend/node_modules` exists `npm run lint`, `npm run check` and the
generated API client drift check in `frontend/` (fix with `make api-client`), and `tests/unit` (about 3 minutes; it needs
`ffmpeg` on the PATH, like the rest of the suite). Install it once per clone:

```bash
git config core.hooksPath .githooks
```

Skip it for a one-off, scoped exception with `git push --no-verify`. CI
still runs everything regardless, so the bypass is safe.

## Releasing

Every push to `main` publishes `ghcr.io/leejianrong/split-video:latest`
and a `:sha-<short>` tag — that covers ordinary day-to-day changes. To cut
a version people can pin to, bump `version` in `pyproject.toml` and tag
the commit it lands on:

```bash
git tag -a vX.Y.Z -m "X.Y.Z"
git push origin vX.Y.Z
```

Pushing a `vX.Y.Z` tag additionally publishes `:X.Y.Z` and `:X.Y` images (the Docker build includes the
frontend). A wheel or sdist built by hand needs `npm ci && npm run build` in `frontend/` first; check it with
`scripts/check_wheel_contents.py` ([docs/frontend.md](docs/frontend.md#packaging)).
