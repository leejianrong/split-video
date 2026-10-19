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

![The visual editor: a video player above a zoomable timeline showing a waveform, labeled colored segment bands and split markers, with a segment table below for naming and choosing what to export](docs/editor-screenshot.png)

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

The editor binds to all interfaces in the container, so anything that can reach
the published port can use the API. To require an API token (for agents; the
bundled UI can't use it, see [API token](#api-token-agents-and-hosted-use)),
add `-e SPLIT_VIDEO_TOKEN=...` to the `docker run`.

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

- **Opening a video for the first time** shows a one-time setup step:
  optionally detect splits by silence (the same thresholds `split` uses —
  see [Key options](#key-options-split) below), and/or kick off audio
  analysis (see **Analyze audio** below) in the background while you start
  editing. Skip both to start from one segment covering the whole
  recording and split it entirely by hand. This never comes back once
  you've gotten past it — see the save note below.
- The video plays in a real player, with a timeline underneath showing the
  currently-proposed splits. A waveform of the audio renders behind the
  timeline once it's finished decoding, so you can eyeball where the actual
  quiet/loud passages are, not just where a threshold happened to trigger.
- Click or drag anywhere on the timeline to move the playhead — it never
  adds a split by itself. Position the playhead where you want a cut, then
  click **Split** (or press `S`) to add one there. Drag a split point's tab
  to move it, hover it for a delete "×", or click it to select it — a
  **Delete split** button appears, or just press Delete/Backspace. Arrow
  keys nudge a selected split by 0.1s (1s with Shift) for precision beyond
  what dragging gives you.
- Scroll (or a trackpad's vertical two-finger swipe) to zoom the timeline
  in/out, centered on your cursor. A horizontal swipe, Shift+scroll, or the
  scrollbar pans instead; "Fit" resets to the whole recording.
- The segment table below the timeline doubles as the export preview: give
  a segment a label and color, uncheck one to leave it out of the export,
  or type a custom export filename — a live count shows how many files
  you're actually about to write.
- **Analyze audio** runs the recording through YAMNet (a general-purpose
  audio classifier) and colors the timeline by what it hears — music,
  singing, speech, applause/crowd, laughter — so you can visually vet a
  proposed split against more than just where things went quiet. It's a
  background job with a progress bar (classifying a multi-hour recording
  takes real time); the model only needs fetching once (see
  [Development](#development)) and the result is cached per file. Every
  label gets its own lane by default; the icon button next to Fit collapses
  them back to a single row.
- **Export** shows a preview of exactly which files will be written (honoring
  the include/exclude checkboxes and custom names from the segment table),
  then writes them plus `manifest.json` right there, with a progress bar
  while it runs.
- Keyboard shortcuts: `Space`/`K` play/pause, `J`/`L` jump back/forward 5s,
  `S` split at the playhead, Delete/Backspace remove the selected split,
  and `?` lists them all in the editor.
- Your splits, labels, and export choices are saved to a sidecar
  `<video>.split-video-project.json` file next to the video as you go —
  closing the editor and reopening the same recording resumes exactly
  where you left off, no setup step shown again. Silence detection, the
  waveform, and audio classification are separately cached to disk in a
  `.split-video-cache/` folder next to the video; that one's regenerable
  scratch space, not something to check in.

Useful flags: `--host`, `--port` (default `8765`), `--no-browser`, plus the
same `--silence-threshold`/`--min-silence-duration`/`--min-song-length`/
`--silence-padding` as the setup step's starting values — see
`split-video edit --help`.

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

### Project commands (scripting the editor's splits)

The same saved splits the visual editor keeps in `<video>.split-video-project.json`
can be read and changed from the command line — handy for scripts and AI
agents. An edit made here shows up the next time the editor opens that
video, and vice versa. Segments and splits are numbered from 1, as in the
editor's segment table (split 1 is the cut between segments 1 and 2), and
every command takes `--json`:

```bash
split-video propose liveshow.mp4 --min-song-length 60 --apply   # detect splits, save them
split-video segments liveshow.mp4                               # list them
split-video add-split liveshow.mp4 --at 12:34.5                 # seconds or [H:]MM:SS
split-video move-split liveshow.mp4 2 --to 25:10
split-video remove-split liveshow.mp4 3
split-video label liveshow.mp4 1 --label "Opener" --color "#d9822b"
split-video label liveshow.mp4 4 --exclude                      # leave it out of exports
split-video export liveshow.mp4 [--only 1,3]                    # same as the editor's Export
split-video analyze liveshow.mp4                                # YAMNet classification (see Development)
```

`propose` only prints unless you pass `--apply`, which replaces any splits
already saved.

### Library: catalogue a folder of recordings (`library`)

```bash
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video library scan /data
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video library list /data --json
```

`library scan` walks the folder recursively and records every media file as an
asset (duration, size, video and audio, width, height, fps) in
`.split-video-assets.json` next to the media. A second scan probes only new or
changed files, and a 3-hour recording is identified from about 3 MiB of
samples plus one ffprobe, not read in full. Files that are not readable media
are listed as skipped with a reason. `library list` never probes. The editor
queues the same scan as a `library_scan` job (`POST /api/v1/library/scan`), and
MCP has `scan_library`.

### Compose: combine clips, overlays, audio and titles (`compose`)

Besides splitting one recording, you can build a video from several files.
A project is a `<name>.split-video-timeline.json` file in the folder holding
your videos. Clips refer to their video by a content-derived asset id
(`a_` plus 16 hex digits, kept in `.split-video-assets.json` in that folder),
so renaming or moving a video does not break a project. In the editor, the
**Compose** button opens a multi-track timeline:

- The **main track** keeps its clips back to back: drag to reorder, drag the
  edges to trim, click the join between two clips to choose a cut or a
  crossfade.
- **Video tracks** above it hold overlays (picture-in-picture) and text
  titles; **audio tracks** hold music or voice-over. Clips there sit at any
  start time, can overlap clips on other tracks but not on their own, and
  can be dragged in time or to another lane. Later video tracks draw on top.
- Select a clip to set its volume and mute, position, size and opacity (for
  overlays), brightness/contrast/saturation, or a title's words, size and
  colour.
- Play previews the main track; **Render** writes an mp4 next to your
  sources with everything mixed in (overlays, titles and all audio).

The same project can be built from the command line. Every command takes
`--json` and `--root DIR` (default: the current directory); clips are numbered
from 1 within their track, and tracks from 1 in the order `show` lists them
(1 is the main track, which is also the default `--track`):

```bash
split-video compose new mix
split-video compose add-clip mix gig.mp4 --in 0:10 --out 1:30
split-video compose add-clip mix encore.mp4
split-video compose transition mix 2 --kind crossfade --duration 1.5
split-video compose add-track mix --kind video                     # track 2
split-video compose add-clip mix crowd.mp4 --track 2 --start 0:20 # picture-in-picture
split-video compose set mix 1 --track 2 --scale 0.3 --x 0.8 --y 0.2 --opacity 0.9
split-video compose add-text mix "Live at the Hall" --start 2 --duration 4
split-video compose add-track mix --kind audio                     # track 3
split-video compose add-clip mix music.mp3 --track 3 --start 0
split-video compose set mix 1 --track 3 --gain 0.4
split-video compose set mix 1 --brightness 0.1 --saturation 1.2   # colour, main track
split-video compose show mix
split-video compose render mix                                     # writes mix-render.mp4
```

Clips reference media by asset id, so a renamed or moved file is found again
(an id comes from hashing the file's size and its first, middle and last MiB).
`compose assets` lists the ids, and `compose add-clip mix --asset a_0123456789abcdef`
adds a clip by id instead of by path. Projects made before asset ids existed
open as they were: their clips are linked automatically, and the first edit
(or `compose migrate mix`) rewrites the file in the new format. A clip whose
file is missing stays in the project and is linked once the file is back.

Other commands: `list`, `assets`, `migrate`, `trim`, `move` (reorder the main track), `place`
(move a clip in time or to another track), `remove`, `remove-track`,
`move-track`, `set-text`. Text needs a TrueType font: the Docker image has one;
elsewhere install e.g. `fonts-dejavu-core` or point `SPLIT_VIDEO_FONT` at a
`.ttf` file.

### Revisions: editing the same project from two places

Every project (split points and compose timelines) carries a revision
number, stored in its file next to `version`. A project that was never saved,
or whose file predates revisions, is revision 0; each save adds 1. The
editor, `show`/`segments` output, `--json`, MCP results and API responses all
report it.

Writes can say which revision they are based on (`base_revision`). If the
project has changed since, the write is refused and nothing is saved:

- **CLI**: `--base-revision N` on every command that edits a project. A stale
  write exits with code 3 and tells you the current revision and how to retry
  (run `split-video compose show NAME` or `split-video segments VIDEO`, re-apply
  your change, retry with `--base-revision <current>`); with `--json` the
  error is also printed as `{"error": "revision_conflict", "current_revision": ...}`.
- **MCP**: every editing tool takes an optional `base_revision`; a stale write is
  an error whose text carries `{"error": "revision_conflict", "current_revision": N, ...}`
  and retry guidance.
- **HTTP**: send `If-Match: "N"` on any write (bodies and `DELETE` alike). A stale
  write gets `409` with `{"detail": ..., "error": "revision_conflict",
  "current_revision": N, "base_revision": M}`. Other 409s (output exists, project
  exists) have no `error` field.
- **The editors** send the revision they last saw with every save. If something
  else changed the project meanwhile, the page reloads the latest version and
  shows "Project changed elsewhere; reloaded the latest version, please redo
  your last change."

**`base_revision` is optional by design.** Leave it out and the edit is applied
to the latest version: the load, change and save happen under a lock, so
concurrent edits from the CLI, MCP and the editor never lose each other's
changes, but nothing checks that you were looking at the latest. Pass it when
your change only makes sense on the version you saw (an agent that read the
project, reasoned about it, then edits by clip number, for example).

The lock is an advisory `flock` on an empty `<project file>.lock` file right
next to each project file (so it lives in the bind-mounted video folder under
Docker; it is git-ignored and safe to delete when nothing is running). It
works across processes on the same machine, and across containers sharing a
bind mount on a Linux host. It does not work where `flock` does not (Windows,
some network filesystems such as NFS without lock support, and some
Docker Desktop file shares): there writers in different processes can race;
the revision check still catches stale writers that pass `base_revision`, but
two simultaneous writes to the same file could drop one.

### MCP server (for AI agents)

`split-video mcp` runs a [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio, so an agent can do everything the project commands above
do — propose splits, review and adjust them, label segments, optionally
classify the audio, and export — without a browser or a running editor. It
reads and writes the same project file, so a human can open the result in
the visual editor afterward. Tools: `list_videos`, `list_segments`,
`propose_splits`, `add_split`, `move_split`, `remove_split`, `label_segment`,
`analyze_audio`, `export`. The `compose` commands above have matching tools:
`list_timelines`, `scan_library`, `list_assets`, `create_timeline`, `show_timeline`, `add_clip_to_timeline`
(a path or an asset id),
`trim_timeline_clip`, `move_timeline_clip`, `remove_timeline_clip`,
`set_timeline_transition`, `add_timeline_track`, `remove_timeline_track`,
`move_timeline_track`, `place_timeline_clip`, `set_timeline_clip`,
`add_timeline_text`, `set_timeline_text`, `render_timeline`.

Tools only touch files under `--root` (default: the current directory):

```bash
claude mcp add split-video -- split-video mcp --root ~/recordings
```

or with Docker (the `-i` keeps stdin open for the stdio transport):

```bash
claude mcp add split-video -- docker run -i --rm -v ~/recordings:/data \
  ghcr.io/leejianrong/split-video mcp --root /data
```

You can keep the visual editor open while an agent edits the same project:
see [Revisions](#revisions-editing-the-same-project-from-two-places) below.

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

### HTTP API

The editor's JSON API is versioned under `/api/v1` (media at
`/api/v1/media/source?path=...`). Its OpenAPI schema is committed as
[`openapi.json`](openapi.json) at the repo root, and CI fails if it drifts
from the code. After changing a route or schema, regenerate it with
`make openapi`. The old unversioned paths (`/api/...`, `/media/source`,
`/media/file`) still work for one release as deprecated aliases (they send a
`Deprecation: true` header and are not in the schema).

Project state responses carry a `revision`, and writes take an optional
`If-Match` header; see [Revisions](#revisions-editing-the-same-project-from-two-places).

`GET /api/v1/events` is a server-sent-events stream of job progress and project
revision changes, including edits made by the CLI or MCP server in another
process (`curl -N localhost:8765/api/v1/events`). The polling endpoints keep
working; the frame format is in the route's description in the schema.

#### API token (agents and hosted use)

By default the server is a local single-user tool and the API is open. Start
it with `SPLIT_VIDEO_TOKEN` set and every request under `/api/` and `/media/`
(including the event stream and the deprecated aliases) must carry
`Authorization: Bearer <token>`, or it gets a `401` with a `WWW-Authenticate:
Bearer` header and the body `{"detail": "...", "error": "unauthorized"}`. The
static page, `/openapi.json` and the docs stay public. `GET /api/v1/whoami`
returns the caller (`id`, `name`, `kind`, `auth_required`), which is an easy
way for an agent to check its token works:

```bash
SPLIT_VIDEO_TOKEN=choose-a-long-random-value split-video edit ~/recordings
curl -H "Authorization: Bearer $SPLIT_VIDEO_TOKEN" localhost:8765/api/v1/whoami
```

An unset or blank value means no checking, and the CLI and the MCP server
(local processes) never need a token. The server prints a line at startup
when token authentication is on, never the token. With a token, each caller
only sees its own jobs (the job's `owner`).

**Known limitation:** browsers cannot attach an `Authorization` header to
`<video src>` or `EventSource`, and the bundled web UI sends no token, so with
a token configured the bundled UI cannot use the API (the page loads, its API
calls get `401`). Token mode is for agents, API clients and hosted
deployments; UI support (for example a session cookie) comes with the
frontend rebuild. Project (revision) events on the event stream do not carry
an owner yet and go to every authenticated client.

Contributing? See [CONTRIBUTING.md](CONTRIBUTING.md) for running tests,
the pre-push hook, and cutting a release.

## License

[Apache License 2.0](LICENSE).

## Questions or issues?

Open a [GitHub issue](https://github.com/leejianrong/split-video/issues).
