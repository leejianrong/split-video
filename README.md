# split-video

Split a long recording of back-to-back songs (e.g. a live performance) into
one file per song, by detecting the silence gaps between them.

## Install

Requires `ffmpeg`/`ffprobe` on your `PATH`.

```bash
uv sync
```

Or, without installing anything locally, use the Docker image (see
[Docker](#docker) below).

## Usage

Always start with `--dry-run` to check the detected segments before writing
any files — live recordings have a non-zero noise floor, so the right
`--silence-threshold` varies per recording:

```bash
uv run split-video liveshow.mp4 --dry-run
```

Once the segments look right, drop `--dry-run` to actually split:

```bash
uv run split-video liveshow.mp4
```

This writes `01 - liveshow.mp4`, `02 - liveshow.mp4`, ... plus a
`manifest.json` into `liveshow_split/` next to the source file.

### Key options

- `--silence-threshold` (default `-35dB`): how quiet counts as silence.
  Try a range from `-30dB` to `-40dB` depending on the recording's noise floor.
- `--min-silence-duration` (default `2.0`): how long a quiet passage must be
  to count as a gap between songs.
- `--min-song-length` (default `30.0`): segments shorter than this get
  merged into a neighboring song — guards against a quiet bridge inside a
  song being mistaken for a gap.
- `--precise`: re-encode for frame-accurate cuts (slower). By default,
  splitting uses a fast stream copy, which can land up to a couple of
  seconds off since it snaps to the nearest keyframe.
- `--output-dir`, `--format`, `--overwrite`, `--manifest/--no-manifest`,
  `-v/--verbose`: see `uv run split-video --help`.

## Docker

A prebuilt image is published to GHCR on every push to `main`. Run it by
mounting the folder that contains your video to `/data`, then passing the
filename (and any flags) exactly as you would locally:

```bash
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video:latest liveshow.mp4 --dry-run
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video:latest liveshow.mp4
```

Split files and `manifest.json` land back in that same host folder. On
Linux, add `--user "$(id -u):$(id -g)"` to the `docker run` command so the
output files are owned by you instead of root:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/data ghcr.io/leejianrong/split-video:latest liveshow.mp4
```

To build the image locally instead of pulling it:

```bash
docker build -t split-video .
docker run --rm -v "$PWD":/data split-video liveshow.mp4 --dry-run
```

## Tests

```bash
uv run pytest
```
