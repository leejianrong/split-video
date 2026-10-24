---
name: split-video
description: Turn a long recording (stream, gig, lecture) into a highlights reel and vertical shorts with the split-video MCP tools. Use when asked to find the best moments of a long video, cut a highlight reel, make shorts, or split a recording into songs. Gives the order of tool calls that works, the size and cost limits, and how to recover from errors.
---

# split-video for agents

Goal: from one long recording to rendered files, without reading the recording. Work from small, stored summaries
(the media report, search hits, ranked candidates), look at frames only to confirm, and write the whole edit as ONE
plan. Tool names below are the MCP tools of `split-video mcp --root DIR`; every tool is written with `()` after its
name, and every `call` block uses real argument names. Times are seconds. Media is referred to by asset id
(`a_` + 16 hex digits).

## The order that works

1. **Import.** `scan_library()` registers every media file under the root as an asset (incremental: unchanged files are
   not probed again). `list_assets()` shows ids, paths, duration, `has_video`. Optional: `prepare_asset()` makes the
   small proxy and thumbnail strip for visual work (minutes per hour of video; the original is never changed).
2. **Describe.** `media_report()` is one bounded document of every stored signal. With `prepare` true it first makes
   the cheap missing ones (loudness, silence, audio classes). `signals.missing` lists what is absent and how to make it.
3. **Analyse what the goal needs.**
   - Speech content: `transcribe_asset()` (optional extra, slow, see Costs).
   - Music / applause / speech regions: `analyze_audio()` (takes the `video` PATH, e.g. "stream.mp4", not an asset id; about 30 s for 2.5 h; cached, so a repeat is instant).
   - Slide or screen changes: `analyze_scenes()`; text on screen: `ocr_asset()` (needs Tesseract).
4. **Pick moments.** Start with `suggest_highlights()` (ranked, non-overlapping candidates with reasons and
   `title_hint`; it only reads what is stored). Look up specific things with `transcript_search()` or `search_asset()`.
   Check a candidate with `what_is_happening()` (everything known around one time), `transcript_range()` (the words of
   one span) or `get_contact_sheet()` / `get_frame()` (pictures). The tool proposes, you decide.
5. **Plan.** Write one edit plan and call `apply_plan()` with `dry_run` true, read `warnings` and the returned
   `timeline`, then call it again with `dry_run` false. The schema is `get_edit_plan_schema()`; unknown fields are errors.
6. **Vertical.** For a short, make a timeline with the one clip, then `set_clip_crop()` with `aspect` "9:16" (it does the
   arithmetic; `center_x` follows the subject). Render it at a vertical preset.
7. **Captions.** Burned in at render time from the transcript, so `transcribe_asset()` must have run:
   `captions` on `render_timeline()` / `queue_renders()`; styles are in `list_caption_styles()`.
8. **Render.** `queue_renders()` for several outputs (returns at once), then `render_queue_status()` and, if needed,
   `cancel_renders()`. `list_export_presets()` lists the presets (`youtube-1080p`, `vertical-1080`, ...).
   `render_timeline()` is the blocking single render.

## Worked example: a highlights reel plus 3 vertical shorts

Assume one 2.5 hour stream, `stream.mp4`, in the root.

```call scan_library
{"folder": ""}
```

Take the asset id from `added` (or `list_assets()`). Then the cheap signals and the overview:

```call media_report
{"asset": "a_0123456789abcdef", "prepare": true}
```

If the goal is speech-driven (a stream, a lecture), transcribe once (about 17 minutes for 2.5 h; the call blocks):

```call transcribe_asset
{"asset": "a_0123456789abcdef"}
```

Rank candidates. Ask for more than you need and drop the weak ones:

```call suggest_highlights
{"asset": "a_0123456789abcdef", "preset": "stream", "count": 8, "min_seconds": 20, "max_seconds": 60, "keywords": ["finally", "wow"]}
```

Confirm the top ones cheaply, never by reading the whole transcript:

```call transcript_range
{"asset": "a_0123456789abcdef", "start": 2000, "end": 2040}
```

```call get_contact_sheet
{"asset": "a_0123456789abcdef", "start": 2000, "end": 2040, "count": 8}
```

Build the reel from the best 5 candidates (their `start` / `end` go straight into `in` / `out`):

```call apply_plan
{"dry_run": true, "plan": {"schema_version": 1, "project": "reel", "mode": "create", "tracks": [{"kind": "video", "clips": [
  {"asset": "a_0123456789abcdef", "in": 2000, "out": 2040},
  {"asset": "a_0123456789abcdef", "in": 1000, "out": 1030, "transition": "crossfade", "transition_duration": 1}]}]}}
```

Read the answer (`duration`, `warnings`; the dry run also returns the whole `timeline`, which can be long); when it is right, send the same call with `dry_run` false. Make
one timeline per short the same way (a single clip each, project `short1`, `short2`, `short3`), then crop each to 9:16:

```call set_clip_crop
{"name": "short1", "clip": 1, "aspect": "9:16", "center_x": 0.5}
```

Queue everything at once. Each render needs its own `output`:

```call queue_renders
{"renders": [
  {"project": "reel", "preset": "youtube-1080p", "output": "reel.mp4"},
  {"project": "short1", "preset": "vertical-1080", "captions": "shorts", "output": "short1.mp4"},
  {"project": "short2", "preset": "vertical-1080", "captions": "shorts", "output": "short2.mp4"},
  {"project": "short3", "preset": "vertical-1080", "captions": "shorts", "output": "short3.mp4"}]}
```

Check progress now and then (see "Do not" below), and read each job's `state`, `error` and `result`:

```call render_queue_status
{"batch_id": "BATCH-ID-FROM-QUEUE_RENDERS"}
```

To stop: `cancel_renders()` with the same `batch_id` (or one `job_id`).

## Sizes, paging and cursors

- **One MCP result stays under 64 KB.** Tools that can return per-item data page it: `analyze_audio()` (`regions`, limit
  200, max 500), `get_transcript()` (`segments`, about 28 KB a page), `transcript_range()`, `transcript_search()`,
  `search_asset()`, `get_ocr()`. `media_report()` and `timeline_index()` shrink themselves and say how in `notices`.
- A paged result has `next_cursor`, `truncated` and `notices`. While `next_cursor` is not null, call again with the
  SAME arguments plus `cursor`. Each item appears exactly once. A cursor is refused (`request.invalid`) after the
  data was re-made: start over without it.
- Whole-recording totals (`total_segments`, `seconds_by_bucket`, ...) are on every page, so you rarely need to page.
- Narrow first: pass `start` / `end` rather than paging through hours.

## Costs, measured on this project's CPU (16 cores, no GPU)

| Step | 2.5 h recording | Note |
| --- | --- | --- |
| `transcribe_asset()` | about 17 min | Blocks. Needs the optional extra (`uv sync --extra transcribe`, or the Docker image built with `WITH_TRANSCRIPTION=1`); without it, making a transcript fails with `extra.transcription_missing` and nothing else breaks. Run it once; a current transcript is returned as is (`made` false), even without the extra. |
| `analyze_audio()` | about 30 s | Cached next to the video. |
| `media_report()` with `prepare` | seconds to minutes | Loudness and silence are quick, audio classes take minutes. |
| `prepare_asset()` | minutes per hour | Only for visual work. |
| each render | about 0.8 GB of RAM | `queue_renders()` runs 2 at once (`--render-concurrency`, max 8). Do not raise it without the RAM. |
| `suggest_highlights()`, searches | well under a second | They only read stored data. |

Long calls block until done. If your client has a tool timeout shorter than the cost above, raise it, or use the HTTP
API where the same work is a job with progress.

## Revisions and conflicts

Every project and timeline has a `revision` in each result. Editing tools take `base_revision`: pass the one you last
read, and a stale write fails with `revision.conflict` and writes nothing. Recover: call `show_timeline()` (or read the
error's `details.current_revision`), re-apply your change on top of what is there, retry with the new `base_revision`.
Replacing a timeline with `apply_plan()` (`"mode": "replace"`) needs `base_revision` (from `show_timeline()`) or
`"replace_unconditionally": true`. `mode` "create" on a name that exists is `plan.invalid` whose `errors` entry is
`plan.project.exists`: pick another name, or replace with `base_revision`.

## Errors

A failed tool has `isError` and a JSON payload `{code, message, fix, details}`. Branch on `code`, do what `fix` says.

| code | React by |
| --- | --- |
| `revision.conflict` | Reload with `show_timeline()`, re-apply, retry with the `current_revision`. |
| `plan.invalid`, `ops.invalid` | Nothing was written. Fix EVERY entry of `errors` (each has `path`, `code`, `hint`), resend. |
| `asset.not_found` | `list_assets()`; a new file needs `scan_library()` first. |
| `extra.transcription_missing`, `extra.ocr_missing`, `model.missing` | Optional piece not installed. Say so to the user with the `fix`; carry on with the other signals. |
| `transcript.missing`, `analysis.missing` | Run the step named in `fix` (`transcribe_asset()`, `analyze_scenes()`, ...), then ask again. |
| `render.output_exists` | Choose another `output`, or `overwrite` true if replacing is intended. |
| `job.unknown` | Batch ids do not survive a server restart: `render_queue_status()` without an id lists what exists. |
| `ffmpeg.failed` | Do not retry blindly: the same input fails the same way. Read the message. |
| `error.internal` | Retry once, then report it with the request. |

The full list is in `docs/errors.md`.

## Do not

- Do not read a whole transcript (`get_transcript()` on hours is dozens of pages). Search, or read a range.
- Do not poll tightly. A render takes minutes: wait 10 to 30 seconds or more between `render_queue_status()` calls.
- Do not apply a plan blind: always `dry_run` first. It writes nothing and returns the whole resulting timeline.
- Do not do dozens of single edits for a reel: one `apply_plan()`.
- Do not start two heavy jobs by hand while a render batch runs (a transcription next to two renders can run a small
  machine out of memory).
- Do not compute crop rectangles yourself: `set_clip_crop()` with `aspect`.
- Do not treat `suggest_highlights()` as truth: confirm the top candidates with text or a frame.
- Do not forget `captions` needs a transcript of each clip's asset.

## Also available

`propose_splits()` (silence-based song splitting for `list_videos()` paths; apply true REPLACES saved splits),
`apply_ops()` (a batch of edits to a timeline, `dry_run` too), `timeline_index()`, `visual_search()`, `ocr_search()`.
CLI: `split-video agent-guide` prints this guide; the MCP server serves it as the resource `guide://agent` and as
prompts `highlights_workflow`, `vertical_short`, `recover_from_error`.
