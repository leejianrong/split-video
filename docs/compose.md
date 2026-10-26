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
split-video compose crop mix 1 --aspect 9:16 --center-x 0.3       # vertical crop of a 16:9 clip (or --x --y --w --h)
split-video compose render mix --size 1080x1920 --overwrite       # a 9:16 short; the crop then fills the frame
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
clip with its own `compose crop` region keeps that crop either way. The same options are `preset` and `fit` on
`POST /api/v1/compose/projects/{name}/render` (list: `GET /api/v1/presets`) and on the MCP tool `render_timeline`
(list: `list_export_presets`). Presets are about how to export, so they are not part of an edit plan.

**Several renders at once** (the highlight reel plus shorts) run as a background queue with limited concurrency:
`split-video compose render-queue batch.json`, `POST /api/v1/compose/render-queue`, MCP `queue_renders`; see
[docs/render-queue.md](render-queue.md).

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
