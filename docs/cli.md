# Command line: split and project commands

Split a long recording of back-to-back songs (e.g. a live performance) into one file per song, by detecting the
silence gaps between them. Songs are trimmed to start and end with music: silence between songs is cut out entirely,
aside from a small protective sliver so a quiet attack or decay isn't clipped.

The `split` command and the project commands (scripting the editor's splits). The other commands are in [library.md](library.md), [analysis.md](analysis.md) and [compose.md](compose.md).

## Split a recording (`split`)

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
[Command-line split (Docker)](docker.md#command-line-split-docker).)

If thresholds alone don't get every split right, use the
[visual editor](editor.md) instead of guessing at flags.

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

## Project commands (scripting the editor's splits)

The same saved splits the visual editor keeps (since SPL-116 in the video's timeline
project, `<video>.split-video-timeline.json` next to the video; see
[the design note](decisions/unified-project-model.md)) can be read and changed from the command line — handy for scripts and AI
agents. An edit made here shows up the next time the editor opens that
video, and vice versa. Segments and splits are numbered from 1, as in the
editor's segment table (split 1 is the cut between segments 1 and 2), and
every command takes `--json`. An old `<video>.split-video-project.json` opens as the
same segments, labels and exclusions and is migrated by the first edit, which
writes the timeline file next to it and leaves the old file untouched:

```bash
split-video propose liveshow.mp4 --min-song-length 60 --apply   # detect splits, save them
split-video segments liveshow.mp4                               # list them
split-video add-split liveshow.mp4 --at 12:34.5                 # seconds or [H:]MM:SS
split-video move-split liveshow.mp4 2 --to 25:10
split-video remove-split liveshow.mp4 3
split-video label liveshow.mp4 1 --label "Opener" --color "#d9822b"
split-video label liveshow.mp4 4 --exclude                      # leave it out of exports
split-video export liveshow.mp4 [--only 1,3]                    # same as the editor's Export
split-video analyze liveshow.mp4                                # YAMNet classification (needs the model, see below)
```

`propose` only prints unless you pass `--apply`, which replaces any splits
already saved.

## Run without Docker (uv)

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
