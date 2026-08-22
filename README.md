# split-video

Split a long recording of back-to-back songs (e.g. a live performance) into
one file per song, by detecting the silence gaps between them.

## Install

Requires `ffmpeg`/`ffprobe` on your `PATH`.

```
uv sync
```

## Usage

Always start with `--dry-run` to check the detected segments before writing
any files — live recordings have a non-zero noise floor, so the right
`--silence-threshold` varies per recording:

```
uv run split-video liveshow.mp4 --dry-run
```

Once the segments look right, drop `--dry-run` to actually split:

```
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

## Tests

```
uv run pytest
```
