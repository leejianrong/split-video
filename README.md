# split-video

Split a long recording of back-to-back songs (e.g. a live performance) into
one file per song, by detecting the silence gaps between them. Songs are
trimmed to start and end with music — silence between songs is cut out
entirely, aside from a small protective sliver so a quiet attack or decay
isn't clipped.

The primary way to use it is the visual editor: a video player plus a
zoomable timeline for reviewing and hand-adjusting the proposed splits
before committing to them. A plain `split` command is also available for
scripting a one-shot split without a browser.

## Quickstart (Docker)

No Python/uv/ffmpeg setup required — the image is published on GHCR
automatically on every push to `main`. If you've cloned this repo, `make
dev` does the same thing but builds the image locally instead of pulling it
(see [Development](#development)):

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

### Command-line split (Docker)

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

## Usage

### Visual editor (`edit`)

`split-video edit ~/recordings` starts a local server and opens a browser
tab with a file browser to pick a video from. Point it at a file directly
(`split-video edit liveshow.mp4`) to skip the picker and open straight into
the editor for it.

In the editor:

- The video plays in a real player, with a timeline underneath showing the
  currently-proposed splits. A waveform of the audio renders behind the
  timeline once it's finished decoding, so you can eyeball where the actual
  quiet/loud passages are, not just where a threshold happened to trigger.
- Click or drag anywhere on the timeline to move the playhead — it never
  adds a split by itself. Position the playhead where you want a cut, then
  click the `][` button (or press `S`) to add one there. Drag a split
  point's tab to move it, hover it for a delete "×", or click it to select
  it — a Delete-split button appears, or just press Delete/Backspace.
  Arrow keys nudge a selected split by 0.1s (1s with Shift) for precision
  beyond what dragging gives you.
- Scroll to zoom the timeline in/out, centered on your cursor; Shift+scroll
  or the scrollbar pans; "Fit" resets to the whole recording.
- Four sliders mirror `split`'s thresholds (see [Key options](#key-options-split)
  below). Moving `min-silence-duration`, `min-song-length`, or
  `silence-padding` recomputes the splits live. Moving `silence-threshold`
  requires clicking **Recompute** — it's the one parameter that needs a
  real (and, for a long recording, potentially slow) ffmpeg pass, so it's
  not tied to every slider drag.
- **Any recompute — live or via the Recompute button — replaces the entire
  split list, including manual edits you've made.** Tune thresholds first,
  then fine-tune by hand last.
- **Analyze audio** runs the recording through YAMNet (a general-purpose
  audio classifier) and colors the timeline by what it hears — music,
  singing, speech, applause/crowd, laughter — so you can visually vet a
  proposed split against more than just where things went quiet. It's a
  background job with a progress bar (classifying a multi-hour recording
  takes real time); the model only needs fetching once (see
  [Development](#development)) and the result is cached per file.
- **Export** writes the split files and `manifest.json` right there, with a
  progress bar while it runs.

Useful flags: `--host`, `--port` (default `8765`), `--no-browser`, plus the
same `--silence-threshold`/`--min-silence-duration`/`--min-song-length`/
`--silence-padding` as starting values — see `split-video edit --help`.

### Command line (`split`)

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
`manifest.json` into `liveshow_split/` next to the source file. (Drop the
leading `split-video` and run these as
`docker run ... ghcr.io/leejianrong/split-video split ...` if you're using
the Docker image instead of a local install — see
[Command-line split (Docker)](#command-line-split-docker) above.)

If thresholds alone don't get every split right, use the
[visual editor](#visual-editor-edit) instead of guessing at flags.

#### Key options (`split`)

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

## Development

Requires `ffmpeg`/`ffprobe` on your `PATH`. Install and run locally with `uv`:

```bash
uv sync
uv run split-video split liveshow.mp4 --dry-run
uv run split-video edit liveshow.mp4
```

The editor's **Analyze audio** feature needs the YAMNet model, which isn't
vendored (same reasoning as `/videos` — no large binaries in git). The
Docker image fetches it automatically at build time; for a local `uv run`
setup, fetch it once yourself:

```bash
make fetch-model
```

To build the Docker image locally instead of pulling it:

```bash
docker build -t split-video .
docker run --rm -v "$PWD":/data split-video split liveshow.mp4 --dry-run
```

Or bring up the editor with one command via the Makefile — it builds the
image and runs the container for you, mounting the current directory
(override with `DIR=`) and publishing the editor on `localhost:8765`
(override with `PORT=`). Run `make` with no target to list what's available:

```bash
make dev
make dev DIR=~/recordings PORT=9000
```

### Tests

```bash
uv run pytest
```

A few tests exercise real YAMNet inference and skip automatically if you
haven't run `make fetch-model`.

### Pre-push hook

A git hook mirrors the fast parts of CI (unit tests, no `ffmpeg` required)
before every push. Install it once per clone:

```bash
git config core.hooksPath .githooks
```

Skip it for a one-off, scoped exception with `git push --no-verify`. CI
still runs the full suite regardless, so the bypass is safe.
