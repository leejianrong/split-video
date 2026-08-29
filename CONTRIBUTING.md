# Contributing

See the [Development](README.md#development) section of the README for how
to build and run split-video locally. This file covers the rest of the
contributor workflow: running tests and the pre-push hook.

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
