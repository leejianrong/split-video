# Render queue: several outputs from one workspace

One instruction can yield the highlight reel and several shorts. A batch is a list of render requests; each becomes a
`render` job on the ordinary [job queue](../src/split_video/jobs.py) and runs in the background, a few at a time. The
engine is `src/split_video/render_queue.py`; HTTP, CLI and MCP are thin adapters and report the same JSON.

| Surface | Queue | Status | Cancel |
| --- | --- | --- | --- |
| HTTP | `POST /api/v1/compose/render-queue` `{"renders": [...]}` | `GET /api/v1/compose/render-queue/{batch_id}`; `GET /api/v1/compose/render-queue` for every render | `POST /api/v1/compose/render-queue/{batch_id}/cancel`; one render: `POST /api/v1/jobs/{job_id}/cancel` |
| CLI | `split-video compose render-queue SPEC.json [--concurrency N] [--json]` runs the batch and waits | prints each render as it ends, then a summary (`--json`: the status document) | Ctrl+C cancels what is queued and running |
| MCP | `queue_renders(renders)` returns at once | `render_queue_status(batch_id?)` | `cancel_renders(batch_id? or job_id?)` |

The CLI is a single process, so it has no separate status or cancel command: it queues, waits and reports (exit code 1 if
any render failed or was cancelled). Use the HTTP API or MCP when the batch should outlive the command.

## A request

Each entry is the options of a single render (`compose render`, `render_timeline`) plus the timeline's name:

```json
{"renders": [
  {"project": "reel",   "preset": "youtube-1080p", "output": "reel-1080.mp4"},
  {"project": "short1", "preset": "vertical-1080", "fit": "crop", "captions": "shorts", "output": "short1.mp4"},
  {"project": "short2", "preset": "vertical-1080", "output": "short2.mp4"}
]}
```

Keys: `project`, `output`, `overwrite`, `width`, `height`, `preset`, `fit`, `captions`, `caption_range`,
`caption_font_size`, `caption_highlight_color`, `caption_position`, `caption_tracks`. A render's range is its
timeline's: cut a timeline to the part you want (or use an [edit plan](edit-plan.md)), then render it. At most 50 per
batch. Give each render of one timeline its own `output`; two requests for the same file in one batch fail the second.

## The answer

```json
{"batch_id": "...", "concurrency": 2, "total": 3, "finished": false,
 "counts": {"queued": 1, "running": 2, "done": 0, "failed": 0, "cancelled": 0},
 "jobs": [{"index": 1, "job_id": "...", "output": "reel-1080.mp4", "state": "running", "progress": 0.42,
           "error": null, "result": null, "started_at": 0, "finished_at": null}]}
```

`jobs` has one entry per request, in request order. `state` is `queued`, `running`, `done`, `failed` or `cancelled`
(the generic job API says `error` for `failed`). A finished render carries `result`, the same document a single render
returns (`name`, `output`, `duration`, `preset`, `fit`, `width`, `height`, `fps`, `captions`). A failed one carries
`error: {code, message, fix}`, where `code` is a stable code of the [error catalogue](errors.md)
(`timeline.not_found`, `render.output_exists`, `source.not_found`, `ffmpeg.failed`, ...) and `fix` says what to do next. `job_id` also works with `GET /api/v1/jobs/{id}` and the SSE stream
(`group` is the batch id).

## Behaviour

- **Concurrency is bounded and enforced by the queue.** At most `concurrency` renders run at once (default 2, hard
  limit 8): `SPLIT_VIDEO_RENDER_CONCURRENCY`, `split-video edit --render-concurrency N`, `split-video mcp
  --render-concurrency N`, `compose render-queue --concurrency N`. The server's job pool has one more worker than that,
  so a full render queue does not keep a proxy or an analysis from starting. Single renders
  (`POST /compose/projects/{name}/render`) count against the same limit. ffmpeg renders are memory-heavy; raise the
  number only when there is RAM for it.
- **Failure is isolated.** A request that cannot start (unknown timeline, bad preset or fit, output exists, bad caption
  option) comes back already `failed`, in its place. One that fails while rendering (a missing source file, ffmpeg
  error, a hung ffmpeg killed by the watchdog) ends `failed`; the rest of the batch carries on.
- **Cancel is real.** A queued render never starts. A running one has ffmpeg terminated (the answer waits a few
  seconds for it). Cancelled and failed renders delete their partial output; an output that existed before is only
  touched once ffmpeg starts writing, and only with `overwrite`.
- Timelines are read when the batch is queued: editing one afterwards does not change a render already queued.
- The queue lives in memory in the server (or MCP) process; batch ids are good until restart.

## Export modes: segments, timeline, highlights

Splitting is an export mode, not a separate tool. One operation writes the files of a mode as a batch on this queue
(`src/split_video/export_modes.py`), so it has the same concurrency limit, states, cancel and failure isolation, and
the same answer plus `mode`. Poll and cancel with the render-queue calls above.

| Surface | Call |
| --- | --- |
| HTTP | `POST /api/v1/compose/export` `{"mode": ..., ...}` |
| CLI | `split-video compose export MODE [--video F] [--project NAME] [--timelines A,B] [--only 1,3] [--preset P] [--fit F] [--captions S] [--output-dir D] [--overwrite] [--concurrency N] [--json]` (waits, exit 1 if any file failed) |
| MCP | `export_project(mode, video?, project?, timelines?, only?, preset?, ...)` returns at once |

| `mode` | Needs | Writes |
| --- | --- | --- |
| `segments` | `video` | Each included segment of the recording's main track as its own file in `<video>_split/`, named by its `export_name`, else its label made safe for a filename (`Song 2.mp4`; the same label twice becomes `Song`, `Song (2)`), else the numbered default of `split-video export` (`NN - <video>`). A stream copy (`precise`, `output_format` as in the split export) unless a preset, size, fit or captions is given: then each segment is rendered to `.mp4` (its clip's crop and gain kept). `only` names segments and overrides the include flags. No `manifest.json` (the old `export` still writes it). |
| `timeline` | `project` (a timeline), or `video` (its own timeline) | The whole timeline as one `<project>.mp4` (a recording's name without its extension), in the root, beside the video for `video`, or in `output_dir`; `output` renames it. Every clip plays, whatever `included` says. It never overwrites a source file. |
| `highlights` | `timelines` | Each listed timeline as a vertical short `<name>.mp4`: preset `vertical-1080`, fit `crop`, the `shorts` captions when a clip's asset has a transcript (`captions: "off"` or any option you pass wins). |

**What "accepted highlight" means.** Project data has no accepted flag. `suggest_highlights` returns candidates and an
[edit plan](edit-plan.md) turns one into a timeline; a highlight is accepted when its timeline is in `timelines`. The
caller (the Highlights panel, an agent) owns that list, so unreviewed suggestions are never exported and no format
changes. A persisted flag (and "export all accepted" without a list) is a later, additive change.

A request that is wrong as a whole (unknown mode, missing `video`/`project`/`timelines`, bad preset or caption option,
more than 50 files) is an error before anything is queued (`export.invalid` and the usual codes). A file that cannot be
written (it exists, unknown timeline, missing source, ffmpeg error) comes back `failed` with `{code, message, fix}` and
the others carry on.
