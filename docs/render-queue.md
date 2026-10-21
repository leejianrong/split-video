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
`error: {code, message}`, where `code` is the exception class (`ProjectNotFoundError`, `OutputExistsError`,
`SourceNotFoundError`, `ExtractError`, ...). `job_id` also works with `GET /api/v1/jobs/{id}` and the SSE stream
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
