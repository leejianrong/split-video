# Running with Docker

The image is published on GHCR on every push to `main`; no Python, uv or ffmpeg setup is needed.

No Python/uv/ffmpeg setup required — the image is published on GHCR
automatically on every push to `main`. If you've cloned this repo, `make
dev` does the same thing but builds the image locally instead of pulling it
(see [Build the image yourself](#build-the-image-yourself) below):

```bash
make dev
```

Or run it directly: mount the folder your recordings live in to `/data`,
publish the editor's port, and point `edit` at that folder. `--host
0.0.0.0` and `--no-browser` are both required in a container — it has no
browser of its own to open, and `127.0.0.1` inside the container wouldn't
be reachable from the host even if it tried:

```bash
docker run --rm -p 8765:8765 -v "$PWD":/data ghcr.io/leejianrong/split-video \
  edit /data --host 0.0.0.0 --no-browser
```

Then open `http://localhost:8765` and pick a video from the file browser —
or point `edit` at a specific file (e.g. `edit /data/liveshow.mp4`) to skip
straight to the editor for it.

The editor binds to all interfaces in the container, so anything that can reach
the published port can use the API. To require an API token (for agents; the
bundled UI can't use it, see [API token](api.md#api-token-agents-and-hosted-use)),
add `-e SPLIT_VIDEO_TOKEN=...` to the `docker run`.

## Command-line split (Docker)

For scripting a one-shot split with no browser involved, `split` works the
same way, minus the port:

```bash
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video split liveshow.mp4 --dry-run
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video split liveshow.mp4
```

Split files and `manifest.json` land back in that same host folder. On
Linux, add `--user "$(id -u):$(id -g)"` so the output files are owned by you
instead of root:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/data ghcr.io/leejianrong/split-video split liveshow.mp4
```

## Build the image yourself

To build the Docker image locally instead of pulling it:

```bash
docker build -t split-video .
docker run --rm -v "$PWD":/data split-video split liveshow.mp4 --dry-run
```

Or bring up the editor with one command via the Makefile: it builds the
image and runs the container for you, mounting the current directory
(override with `DIR=`) and publishing the editor on `localhost:8765`
(override with `PORT=`). Run `make` with no target to list what's available:

```bash
make dev
make dev DIR=~/recordings PORT=9000
```

`make dev` runs the `editor` service of [`docker-compose.yml`](../docker-compose.yml) (`docker compose run --rm`), so
repeated or parallel runs never fight over a container name. The host port is the only thing that can still collide:
pick another with `PORT=`, or skip host ports altogether by routing through a machine-wide Traefik. Copy
[`docker-compose.override.yml.example`](../docker-compose.override.yml.example) to `docker-compose.override.yml`
(gitignored), keep Traefik running with an external network named `proxy`, and `make dev` serves the editor at
`http://split-video.localhost/` with no published port. Without the override file nothing changes.

The optional features are build args: `WITH_TRANSCRIPTION=1` (speech to text, see
[Transcription](analysis.md#transcription-optional)) and `WITH_OCR=1` (on-screen text, see
[On-screen text](analysis.md#on-screen-text-ocr-optional)).
