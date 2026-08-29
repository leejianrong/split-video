# Contributing

See the [Development](README.md#development) section of the README for how
to build and run split-video locally. This file covers the rest of the
contributor workflow: running tests, the pre-push hook, and cutting a
release.

## Tests

```bash
uv run pytest
```

A few tests exercise real YAMNet inference and skip automatically if you
haven't run `make fetch-model`.

## Pre-push hook

A git hook mirrors the fast parts of CI (unit tests, no `ffmpeg` required)
before every push. Install it once per clone:

```bash
git config core.hooksPath .githooks
```

Skip it for a one-off, scoped exception with `git push --no-verify`. CI
still runs the full suite regardless, so the bypass is safe.

## Releasing

Every push to `main` publishes `ghcr.io/leejianrong/split-video:latest`
and a `:sha-<short>` tag — that covers ordinary day-to-day changes. To cut
a version people can pin to, bump `version` in `pyproject.toml` and tag
the commit it lands on:

```bash
git tag -a v0.2.0 -m "0.2.0"
git push origin v0.2.0
```

Pushing a `vX.Y.Z` tag additionally publishes `:X.Y.Z` and `:X.Y` images.
