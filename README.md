# split-video

Split a long recording of back-to-back songs (e.g. a live performance) into
one file per song, by detecting the silence gaps between them. Songs are
trimmed to start and end with music — silence between songs is cut out
entirely, aside from a small protective sliver so a quiet attack or decay
isn't clipped.

Comes with a visual editor: a video player plus a zoomable timeline for
reviewing and hand-adjusting the proposed splits before committing to them.

## Quickstart (Docker)

The easiest way to run split-video is the prebuilt image on GHCR — no
Python/uv/ffmpeg setup required. It's published automatically on every push
to `main`.

Mount the folder containing your video to `/data`, then pass the filename
(and any flags) as you would locally:

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

### Visual editor (Docker)

The editor runs a small local web server, so running it in a container needs
one extra flag and a published port:

```bash
docker run --rm -p 8765:8765 -v "$PWD":/data ghcr.io/leejianrong/split-video \
  edit liveshow.mp4 --host 0.0.0.0 --no-browser
```

Then open `http://localhost:8765` in your own browser — the container has
no browser to open one for you, and `127.0.0.1` inside the container
wouldn't be reachable from the host even if it tried.

## Usage

Always start with `--dry-run` to check the detected segments before writing
any files — live recordings have a non-zero noise floor, so the right
`--silence-threshold` varies per recording:

```bash
split-video split liveshow.mp4 --dry-run
```

Once the segments look right, drop `--dry-run` to actually split:

```bash
split-video split liveshow.mp4
```

This writes `01 - liveshow.mp4`, `02 - liveshow.mp4`, ... plus a
`manifest.json` into `liveshow_split/` next to the source file.

If thresholds alone don't get every split right, use the visual editor
instead of guessing at flags:

```bash
split-video edit liveshow.mp4
```

This opens a browser tab with the video, a timeline showing the proposed
splits, and sliders for the same thresholds below — drag a split point,
add or delete one, zoom in on the timeline for precision, and click Export
when it looks right. (Drop the leading `split-video` and run these as
`docker run ... ghcr.io/leejianrong/split-video split ...` /
`edit ...` if you're using the Docker image instead of a local install —
see [Quickstart](#quickstart-docker) above.)

### Key options (`split`)

- `--silence-threshold` (default `-35dB`): how quiet counts as silence.
  Try a range from `-30dB` to `-40dB` depending on the recording's noise floor.
- `--min-silence-duration` (default `2.0`): how long a quiet passage must be
  to count as a gap between songs.
- `--min-song-length` (default `30.0`): segments shorter than this get
  merged into a neighboring song — guards against a quiet bridge inside a
  song being mistaken for a gap.
- `--silence-padding` (default `0.15`): seconds of near-silence deliberately
  kept on each side of a cut, so a quiet attack or decay transient isn't
  clipped. `--silence-padding 0` gives a hard cut at the exact point ffmpeg
  detected the silence boundary.
- `--precise`: re-encode for frame-accurate cuts (slower). By default,
  splitting uses a fast stream copy, which can land up to a couple of
  seconds off since it snaps to the nearest keyframe.
- `--output-dir`, `--format`, `--overwrite`, `--manifest/--no-manifest`,
  `-v/--verbose`: see `split-video split --help`.

### Visual editor (`edit`)

`split-video edit liveshow.mp4` starts a local server and opens a browser
tab. In it:

- The video plays in a real player, with a timeline underneath showing the
  currently-proposed splits.
- Drag a split point to move it, click empty timeline space to add one, or
  hover a split point for a delete "×" (select it and press Delete/Backspace
  as an alternative). Arrow keys nudge a selected split by 0.1s (1s with
  Shift) for precision beyond what dragging gives you.
- Scroll to zoom the timeline in/out, centered on your cursor; Shift+scroll
  or the scrollbar pans; "Fit" resets to the whole recording.
- Four sliders mirror `split`'s thresholds. Moving `min-silence-duration`,
  `min-song-length`, or `silence-padding` recomputes the splits live.
  Moving `silence-threshold` requires clicking **Recompute** — it's the one
  parameter that needs a real (and, for a long recording, potentially slow)
  ffmpeg pass, so it's not tied to every slider drag.
- **Any recompute — live or via the Recompute button — replaces the entire
  split list, including manual edits you've made.** Tune thresholds first,
  then fine-tune by hand last.
- **Export** writes the split files and `manifest.json` right there, with a
  progress bar while it runs.

Useful flags: `--host`, `--port` (default `8765`), `--no-browser`, plus the
same `--silence-threshold`/`--min-silence-duration`/`--min-song-length`/
`--silence-padding` as starting values — see `split-video edit --help`.

## Development

Requires `ffmpeg`/`ffprobe` on your `PATH`. Install and run locally with `uv`:

```bash
uv sync
uv run split-video split liveshow.mp4 --dry-run
uv run split-video edit liveshow.mp4
```

To build the Docker image locally instead of pulling it:

```bash
docker build -t split-video .
docker run --rm -v "$PWD":/data split-video split liveshow.mp4 --dry-run
```

### Tests

```bash
uv run pytest
```
