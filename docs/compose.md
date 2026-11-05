# Compose: combine clips, overlays, audio and titles

Also covers edit plans and edit operations (`apply-plan`, `apply-ops`).

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
- Play shows the whole composite (overlays, titles and all audio) in the editor's preview, rendered a few seconds
  ahead of the playhead in preview chunks; **Render** writes an mp4 next to your
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
split-video compose crop mix 1 --aspect 9:16 --center-x 0.3       # vertical crop of a 16:9 clip (or --x --y --w --h)
split-video compose render mix --size 1080x1920 --overwrite       # a 9:16 short; the crop then fills the frame
split-video compose set-canvas mix 9:16                            # the project's output ratio: 16:9, 9:16 or 1:1
split-video compose undo mix                                       # and `redo`: step through the edit history (a new revision)
split-video presets                                                # named export destinations
split-video compose render mix --preset vertical-1080 --overwrite  # 1080x1920, 30 fps, tuned encoder
split-video compose render mix --preset youtube-1080p --fit crop --overwrite  # cover the frame instead of black bars
split-video caption-styles                                         # classic, shorts, minimal
split-video compose render mix --preset vertical-1080 --captions shorts --overwrite   # burn in word-highlighted captions
split-video compose render mix --captions classic --caption-range 10-40 --overwrite   # only part of the timeline
```

**Export presets** name a destination instead of codec flags: `youtube-1080p` (1920x1080, 30 fps),
`vertical-1080` (1080x1920, 30 fps), `square-1080` (1080x1080, 30 fps) and `draft-720` (1280x720, 24 fps, fastest
encoder and lower quality, for quick review renders). All are mp4 with H.264/AAC and `+faststart`; `split-video presets`
(or `--json`) shows every setting. Precedence for the frame size is `--size` over `--preset` over the first clip's,
and without a preset renders are as before. When a clip's aspect differs from the frame, `--fit letterbox` (default)
scales it to fit and pads with black bars, `--fit crop` scales it to cover the frame and cuts the overflow, centered; a
clip with its own `compose crop` region keeps that crop either way. A clip can override the choice for itself with `compose set <project> <clip> --fit letterbox|crop|default` (also `fit` on `set_timeline_clip` and the `set_clip_properties` op). The same options are `preset` and `fit` on
`POST /api/v1/compose/projects/{name}/render` (list: `GET /api/v1/presets`) and on the MCP tool `render_timeline`
(list: `list_export_presets`). Presets are about how to export, so they are not part of an edit plan.

**Preview one frame** of the edited result without rendering the whole timeline: the main track, overlays, titles, colour
and crop at a time, drawn by the export's own filter graph (cut to that instant, so ffmpeg seeks each source and decodes a
few frames). It takes a fraction of a second for a typical 1080p project (measured 0.2 to 0.3 s, with or without proxies)
and is synchronous, not a job. Captions and sound are not drawn.

```bash
split-video compose preview-frame mix 12.5 --width 640 --out look.jpg     # prints JSON: project, revision, time, size, source, out
split-video compose preview-frame mix 1:02 --preset vertical-1080 --width 270 --format png
split-video compose preview-frame --video talk.mp4 12.5     # a recording's own timeline, the one the editor shows
curl -o look.jpg "localhost:8765/api/v1/compose/projects/mix/preview-frame?time=12.5&width=640"
```

A recording's own timeline (the project the editor has open) is addressed by the video's path instead of a name:
`GET /api/v1/project/preview-frame?path=talk.mp4&time=12.5`, `compose preview-frame --video talk.mp4 12.5` and the MCP
tool `preview_frame` with `video` (give `name` or `video`, not both). The answer is the same.

The same options (`time`, `width`, `preset`, `fit`, `format`, `quality`, `source`) are on
`GET /api/v1/compose/projects/{name}/preview-frame` and the MCP tool `preview_frame`, which all say the same thing about
the frame: `project`, `revision` (of the timeline it came from), `time` (the frame shown: the requested time on the
output's frame grid, at most one frame earlier), `requested_time`, `width`, `height` (the export's aspect ratio; `width`
defaults to 640 and is never above the export's), `mime`, `format`, `source`, `duration` and `bytes`. The CLI adds `out`; the
HTTP route sends them as `X-Preview-*` headers and the image as the body (`image/jpeg` or `image/png`) with an `ETag` that
hashes the revision and the request, so `If-None-Match` gets a `304` without a render while the timeline has not changed;
MCP returns an image block plus the JSON text block (width at most 1280, as the image is inlined). `source=auto` (default)
reads a clip's proxy when it has one (right to within 1/15 s), `original` reads the files like the export, which never
reads a proxy. A time past the end is `time.out_of_range`.

How close it is to the export: with `source=original` and the same size, the frame equals the export's frame at that
time up to the H.264 encoding of the export, which the golden-frame tests below pin down. The frame
after a cross-fade is on time too: the render graph pads the outgoing clip with one cloned frame, because ffmpeg 4.4's
`xfade` otherwise drops a frame and shows the incoming clip one frame late (SPL-123).

### Preview a range as a video

**Preview a range** to judge transitions and pacing, which need motion: a low-resolution mp4 of up to 30 s of the
edited result, drawn by the same code as the export and the single frame (the project is cut to the window, so ffmpeg
seeks each source and decodes only the chunk). All tracks are drawn and all audio is mixed (clip gain and mute included);
captions are not. It is slow work, so over HTTP it is a `preview_range` job (cancel and progress as for any job); the CLI
and MCP tool run it inline.

```bash
split-video compose preview-range mix 12 22 --height 360      # prints JSON: key, path of the mp4, start, end, size, source, bytes, cached
split-video compose preview-range --video talk.mp4 12 22      # a recording's own timeline
curl -X POST localhost:8765/api/v1/compose/projects/mix/preview-range -H 'content-type: application/json' \
     -d '{"start": 12, "end": 22}'                             # {job_id, cached, key, revision, result}
curl localhost:8765/api/v1/jobs/JOB_ID                         # state done: result is the chunk
curl -o chunk.mp4 localhost:8765/api/v1/compose/projects/mix/preview-range/KEY   # video/mp4, Range supported
```

The chunk is H.264 (Constrained Baseline, `ultrafast`, crf 30) and AAC 64 kbit/s in an mp4 with `+faststart`, which every
browser plays and seeks in without downloading it first. It is `height` pixels high (default 480, at most 720, never above
the export's) with the export's aspect ratio and frame rate (`preset` and `fit` are the export's). `source=auto` (default)
reads clips' proxies where they exist (15 fps and mono audio, so motion is coarser), `original` reads the files. A range
longer than 30 s is `preview.range_too_long` (400 over HTTP): ask for consecutive ranges. 30 s keeps one render to a few
seconds and one file to a few MB; an `end` past the timeline is cut to it. A `start` inside a cross-fade is exact: ffmpeg
renders from the fade's start and the lead-in is cut off.

Every surface says the same thing: `project`, `revision`, `key`, `media` (the chunk's opaque media id), `path` (where the
mp4 is, `display_path` of the media id), `mime`, `start`, `end`, `duration`, `width`, `height`, `fps`, `source` (`proxy`,
`original` or `mixed`), `bytes` and `cached`. MCP (`preview_range`) returns this reference and never the video bytes. A
recording's own timeline is `POST /api/v1/project/preview-range?path=` and `GET /api/v1/project/preview-range/{key}?path=`,
`compose preview-range --video` and MCP `video`.

The editor plays by chaining chunks on a fixed grid: 8 s ranges starting at multiples of 8 s, so replays and seeks ask for
ranges that are already cached, and it keeps the one playing and two ahead (3 of the 8 cached chunks; the other 5 are the
ones just played, so a short rewind is a cache hit). See [the frontend docs](frontend.md#playback).

The cache lives in the video's `.split-video-cache/` sidecar (for a named project, next to its `<name>.timeline` anchor),
never under `~/.cache`. The key hashes the project revision, the range and the options, so any edit gives new keys: the
old key answers `404` and the old files are deleted when the next chunk is stored. Asking again for the same range
before an edit answers at once (`cached: true`, no ffmpeg; a job that is already `done`). At most 8 chunks are kept per
project, least recently used first out (about 20 MB at 30 s each). The key does not cover the source files: replacing a
source without editing the project keeps serving the old chunk.

Measured on a 1080p project (two 40 s main clips with a cross-fade, a picture-in-picture, a title; `source=original`,
854x480): a 10 s range renders in 1.8 s (930 KB), a 30 s range in 5.7 s (2.4 MB; test footage is noisier than most);
a cache hit takes under a millisecond in the engine.

### Golden-frame tests

`tests/unit/test_golden_frames.py` (harness in `tests/unit/golden.py`) renders a small project per feature, exports
it, and compares the preview's frame with the export's frame at the same time and size. Fixtures are made with ffmpeg
`lavfi` at test time. Cases: overlay (position, scale, opacity), a clip with its own fit, a placed main-track clip, cross-fade (before, during, after), colour grade and
title text. A case passes when the mean absolute difference per channel and the share of samples off by more than 24 are
under its `Tolerance`, and, for moving footage, the frame is 3 times closer than the export's frame 0.2 s later.

`test_the_canvas_preset_gives_the_export_frame` does the same for the canvas presets the top bar offers (vertical and
square): the preview at the preset against the export at the preset's own size. `test_cli_and_mcp_exports_without_a_preset_match_the_canvas_preview` runs `export_modes.submit_export` (what the CLI, API and MCP call) on a project with a 9:16 canvas, timeline and segments modes, and compares it to the preview; with preset `original` the segment is still a 640x360 stream copy.

Run them with `uv run pytest tests/unit/test_golden_frames.py -s` (about 20 s; the `-s` prints each measurement). A
failure reads `mean difference 3.4 is over 2.5` or `1.8% of the samples differ by more than 24`: the preview and the
export disagree about that feature at that time. Look at both frames before touching a limit.

The `test_a_changed_renderer_is_reported` cases change one thing on the preview side only (an overlay moved 5 px,
opacity 0.7 to 0.55, contrast 1.4 to 1.3, other text, a shorter fade) and require a failure. On ffmpeg 4.4 an
unchanged frame measures a mean of 0.9 to 1.8 (shares up to 0.07%), the smallest of those changes 3.4 or a share of
1.8%, so each limit sits between the two. To add a case, write a builder in the test module, list its times and
tolerance in `CASES` and add a mutation to `MUTATIONS`.

What this does not catch: preview and export both call `render.build_plan`, so a change there moves both frames and the
comparison still passes. `test_pinned_colours_do_not_drift_in_either_renderer` covers that for colour, opacity blending
and grading with flat sources and fixed pixel values. `test_pinned_text_box_does_not_drift_in_either_renderer` pins
white title text on black (SPL-125): the bright pixels' box must be 224 x 30 px centred on (238.5, 134.5) at 480x270,
within 4 px on the centre, 8 on the width and 3 on the height, in both renderers. The margins allow for a pixel or two
of glyph-hinting drift between ffmpeg and font versions and sit just under the smallest change it must catch (text
moved 5 px, or one font-size step that adds 15 px of width); `test_a_moved_or_resized_text_box_is_reported` checks that. Captions and
audio are not drawn by the preview and not compared.

**Several renders at once** (the highlight reel plus shorts) run as a background queue with limited concurrency:
`split-video compose render-queue batch.json`, `POST /api/v1/compose/render-queue`, MCP `queue_renders`; see
[docs/render-queue.md](render-queue.md).

An export with no `--preset` and no size follows the canvas too: `compose export` with mode `timeline` renders at it, and mode `segments` renders each segment at it instead of copying the stream (the top bar's canvas is on the recording's own timeline). To export at the source shape anyway, pass preset `original` (`--preset original`, MCP `preset: "original"`, API `preset`): segments stay a stream copy and a timeline renders at the first clip's size. Projects with no canvas are unchanged. `original` is not a preset in `presets`; modes other than segments and timeline do not take it.

To export a recording's segments, a whole timeline or accepted highlights as shorts in one call: `split-video compose export`,
`POST /api/v1/compose/export`, MCP `export_project` ([export modes](render-queue.md#export-modes-segments-timeline-highlights)).

**Captions** (`--captions <style>`) burn the spoken words into the picture, on top of everything else, with the word
being spoken highlighted. They come from the transcripts of the clips' assets (run `split-video transcribe` first; a clip
whose asset has none is simply not captioned and is listed in the result's `captions.clips_without`), for the main video
track's unmuted clips (`--caption-tracks 1,2` picks other tracks by number), mapped to where each clip sits on the
timeline, crossfades included. Styles are `classic` (white on a dark box, lower third, yellow highlight), `shorts` (large
bold upper-case with an outline, centre-lower inside the safe area of a vertical frame, green highlight) and `minimal`
(small, thin outline); sizes are fractions of the output height, so they scale across presets. `--caption-font-size`
(0.01..0.2 of the height), `--caption-highlight #RRGGBB` and `--caption-position top|middle|bottom` override a style,
`--caption-range START-END` (timeline seconds) limits where captions appear. The same are `captions`, `caption_range`
(`{start, end}`), `caption_font_size`, `caption_highlight_color`, `caption_position` and `caption_tracks` on the render
request and on `render_timeline`; the styles are `GET /api/v1/caption-styles` and `list_caption_styles`, and the render
result gains `captions: {style, clips_with_transcript, clips_without, words, lines, events}`. Captions are not part of an
edit plan or the project file. Rules (line breaking, how a word cut by a clip edge is treated, the highlight events) are in
the docstring of `src/split_video/captions.py`.

Clips reference media by asset id, so a renamed or moved file is found again
(an id comes from hashing the file's size and its first, middle and last MiB).
`compose assets` lists the ids, and `compose add-clip mix --asset a_0123456789abcdef`
adds a clip by id instead of by path. Projects made before asset ids existed
open as they were: their clips are linked automatically, and the first edit
(or `compose migrate mix`) rewrites the file in the new format. A clip whose
file is missing stays in the project and is linked once the file is back.

**Lane toggles.** A track can be locked, hidden or muted (the timeline editor's lane headers): `apply-ops` with
`set_track_flags` (`locked`, `hidden`, `muted`). A locked track refuses every edit of its clips (`track.locked`); a hidden
video track and a muted track are left out of the render (a muted track is silent), the main video track cannot be hidden.
They are saved on the track as optional `locked`, `hidden`, `muted` (written only when on), so the file version stays 2.

**Canvas.** The project's output ratio (16:9, 9:16 or 1:1, the editor's top bar) is set with `compose set-canvas <project> <ratio>`
(`--base-revision`; any other ratio is refused as `timeline.invalid`), `apply-ops` and `set_canvas` (`ratio`), carries revisions like any edit and is saved as an optional `canvas` (written only once chosen). Adding it
made the file format version 3: a version 1 or 2 file migrates on load (no canvas, shown as 16:9) and is written back as
3, on the next edit or `compose migrate`. A render, `preview-frame` or `preview-range` that names no `--preset`
and no size uses the canvas's preset (`youtube-1080p`, `vertical-1080`, `square-1080`); a project that never chose one
keeps the first clip's size. Switching keeps every clip, and so does an `apply-plan` replace, which keeps the canvas. `show`, `GET /api/v1/compose/projects/{name}` and MCP
`show_timeline` return `canvas: {ratio, preset, needs_reframe}`, where `needs_reframe` lists the main-track clips whose
picture (after their crop) is not the canvas's shape and that have no `fit` of their own, so the export letterboxes or
crops them as it is told to.

**Per-clip fit and main-track placement (format version 4).** A media clip's optional `fit` (`letterbox` or `crop`;
absent is `default`, the render's fit) is saved only when set. The main track now honours `x`, `y`, `scale` and
`opacity` like an overlay does: the fitted picture is scaled by `scale`, centred on (`x`, `y`) and faded over black.
These four were stored but ignored there, so the version 3 to 4 migration resets them to neutral on main-track media
clips; nothing drawn before changes. A build that only knows version 3 ignores `fit` and writes version 3 back.

Other commands: `list`, `assets`, `migrate`, `trim`, `move` (reorder the main track), `place`
(move a clip in time or to another track), `remove`, `remove-track`,
`move-track`, `set-text`. Text needs a TrueType font: the Docker image has one;
elsewhere install e.g. `fonts-dejavu-core` or point `SPLIT_VIDEO_FONT` at a
`.ttf` file.

## Edit plans: a whole timeline from one JSON document (`apply-plan`)

Instead of many single edits, write one plan (clips with asset ids and in/out seconds, order, crossfades, titles,
extra tracks) and apply it atomically; a plan with any problem changes nothing and reports every error at once:

```
split-video apply-plan plan.json --root /data [--dry-run] [--base-revision N] [--json]   # or `-` for stdin
split-video plan-schema                                                                 # the JSON Schema
```

Over HTTP it is `PUT /api/v1/compose/projects/{name}/plan` (schema at `GET /api/v1/schemas/edit-plan`); MCP has
`apply_plan` and `get_edit_plan_schema`. The format, error codes and examples are in
[docs/edit-plan.md](edit-plan.md).

## Edit operations: batches of edits with dry-run (`apply-ops`)

To change a timeline that exists, send a list of single edits (`add_clip`, `trim_clip`, `set_transition`,
`add_text_clip`, ...). They apply all or none, in one save; `--dry-run` shows the resulting project and every error
without writing anything:

```
split-video apply-ops edit.json --root /data [--dry-run] [--continue-on-error] [--base-revision N] [--json]
split-video ops-schema                                                                   # the JSON Schema
```

Over HTTP it is `POST /api/v1/compose/projects/{name}/ops?dry_run=` (schema at `GET /api/v1/schemas/edit-ops`); MCP has
`apply_ops` and `get_edit_ops_schema`. The ops, error codes and examples are in [docs/edit-ops.md](edit-ops.md).
