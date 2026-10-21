# Error catalogue

<!-- Generated from src/split_video/error_catalogue.py by scripts/export_error_catalogue.py; edit the registry, then
run `make errors`. -->

Every failure of the CLI, the MCP server and the HTTP API carries the same four fields:

```json
{
  "code": "clip.overlap",
  "message": "clip would overlap another clip on the track (until 12s)",
  "fix": "Place the clip at or after the end of the previous clip ...",
  "details": {}
}
```

- `code` is stable and namespaced (`time.*`, `segments.*`, `clip.*`, `asset.*`, ...). Branch on it, not on `message`.
- `message` says what happened. `fix` says what to do next: a tool or command to call, a value to change, or the
  valid choices. `details` is present when there is structured data (a revision number, a range, the valid choices).
- **CLI**: `Error:` and `Fix:` lines on stderr and a non-zero exit code (`1`; `2` for a time argument that cannot be
  parsed; `3` for `revision.conflict`; `4` for a missing optional extra). With `--json` on the command, or `--json-errors` before the command name, the payload is
  also printed to stdout as JSON.
- **MCP**: the tool result has `isError: true`; its text is `Error executing tool NAME: ` followed by the payload as
  JSON (everything from the first `{`).
- **HTTP**: the response body is `{"detail": <message>, "code", "message", "fix", "details"}`; the status is the one in
  the table (routes keep the status they always had).
- **Render queue and jobs**: a failed render's `error` is `{code, message, fix}`.
- A rejected edit plan or op batch is `plan.invalid` / `ops.invalid`; the body also keeps its `errors` list, each
  entry with its own `path`, `code` (`plan.*` / `ops.*`, see [edit-plan.md](edit-plan.md) and
  [edit-ops.md](edit-ops.md)) and `hint`.

| Code | HTTP | CLI exit | Meaning | Suggested fix |
| --- | --- | --- | --- | --- |
| `time.invalid` | 422 | 1 | A time could not be read as a number of seconds or a clock time. | Give the time as seconds (83.5) or as a clock time (1:23.5 or 1:02:03); {value} is neither. |
| `time.negative` | 422 | 1 | A time, start or in point is below 0. | Use 0 or a later time: the earliest point of any media is 0 s. |
| `time.out_of_range` | 422 | 1 | A time lies outside the media or the editable span. | Use a time within {range}. The media's duration is in `list_assets` (CLI `split-video library list DIR`, HTTP GET /api/v1/assets). |
| `time.range_inverted` | 422 | 1 | A range ends at or before the point where it starts. | Make the end later than the start (start < end, both in seconds) and send the range again. |
| `segments.overlap` | 422 | 1 | Two segments of a split project overlap. | Make each segment start where the previous one ends (no overlap), or remove one with `remove_split`; `list_segments` (CLI `split-video segments VIDEO`) shows the current boundaries. |
| `segments.too_close` | 422 | 1 | A split would sit on top of an existing split. | Choose a time at least a second away from every existing split, or move the existing one with `move_split`; `list_segments` shows where they are. |
| `segments.not_found` | 422 | 1 | A segment or split number does not exist. | Use a number from 1 up to the count `list_segments` (CLI `split-video segments VIDEO`) reports; split N is the cut between segments N and N+1. |
| `segments.invalid` | 422 | 1 | The list of segments breaks a rule (empty, unsorted, duplicate export names). | Send a non-empty list sorted by start time, with unique export names; `list_segments` shows a valid one. |
| `clip.overlap` | 422 | 1 | A clip would overlap another clip on a track that places clips by start time. | Place the clip at or after the end of the previous clip on that track, or on another track; `show_timeline` (CLI `split-video compose show NAME`) lists every clip's start and end. |
| `clip.too_short` | 422 | 1 | A clip would be shorter than the minimum clip length. | Make out_point at least a tenth of a second after in_point (a clip must be at least 0.1 s long). |
| `clip.not_found` | 404 | 1 | No clip or track with that number or id exists. | Use a clip number from 1 (or an id) that `show_timeline` (CLI `split-video compose show NAME`) lists; numbers are per track. |
| `timeline.rule_violated` | 422 | 1 | The edit breaks one of the timeline's editing rules (crossfades, tracks, crop, text, ...). | Read the message for the rule, inspect the tracks with `show_timeline` (CLI `split-video compose show NAME`), then change the offending value; `dry_run` on `apply_ops` checks an edit without writing. |
| `timeline.invalid` | 422 | 1 | An edit to a timeline was refused (bad argument for the timeline). | Read the message, then correct the argument; `show_timeline` (CLI `split-video compose show NAME`) shows the timeline as it is. |
| `timeline.not_found` | 404 | 1 | No timeline project with that name exists. | Use a name `list_timelines` (CLI `split-video compose list`, HTTP GET /api/v1/compose/projects) returns, or create it with `create_timeline` (or `apply_plan` in mode create). |
| `timeline.exists` | 409 | 1 | A timeline project with that name already exists. | Pick another name, or replace the project with `apply_plan` in mode replace and `base_revision` set to its current revision (`show_timeline`). |
| `timeline.name_invalid` | 422 | 1 | The project name is not usable. | Use a plain name of letters, digits, dashes and underscores, with no slashes or extension. |
| `asset.not_found` | 404 | 1 | No asset with that id is in the workspace's asset table. | List the ids with `list_assets` (CLI `split-video library list DIR`, HTTP GET /api/v1/assets); a new file needs `scan_library` (CLI `split-video library scan DIR`) first. Ids are `a_` plus 16 hex digits. |
| `source.not_found` | 404 | 1 | The media file does not exist under the workspace root. | Use a path relative to the root that `list_videos` (CLI `split-video library list DIR`) shows, or run `scan_library` if the file was just added. |
| `source.outside_root` | 400 | 1 | A path resolves outside the workspace root. | Use a path inside the root (no `..`, no absolute path outside it), or start the server or CLI with `--root` set to the folder that holds the file. |
| `media.unreadable` | 422 | 1 | ffprobe could not read the file as media. | Check the file plays (`ffprobe FILE`) and is not still being written; re-export or re-download it if it is damaged. |
| `media.audio_only` | 422 | 1 | The operation needs a picture and the asset is audio-only. | Pick an asset with a video stream (`list_assets` shows has_video), or use an audio-only operation. |
| `revision.conflict` | 409 | 3 | The project changed since the revision the write was based on; nothing was written. | Reload the project ({reload}), re-apply your change on top of it, and retry with base_revision={current_revision} (CLI `--base-revision {current_revision}`, HTTP `If-Match: {current_revision}`). |
| `preset.unknown` | 422 | 1 | The export or highlight preset name is not one of the known presets. | Use one of: {choices}. `list_export_presets` (CLI `split-video presets`, HTTP GET /api/v1/presets) describes them. |
| `fit.unknown` | 422 | 1 | The fit is not one of the known framing modes. | Use one of: {choices}. `letterbox` keeps the whole frame with bars, `crop` fills the frame. |
| `caption.style_unknown` | 422 | 1 | The caption style name is not one of the known styles. | Use one of: {choices}. `list_caption_styles` (CLI `split-video caption-styles`, HTTP GET /api/v1/caption-styles) describes them. |
| `caption.invalid` | 422 | 1 | A caption option (range, position, colour, tracks) is not usable. | Fix the option named in the message: positions are top, middle or bottom; colours look like #RRGGBB; a range is {start, end} in timeline seconds with start < end; tracks are 1-based numbers `show_timeline` lists. |
| `render.output_exists` | 409 | 1 | The render's output file already exists. | Pass overwrite=true (CLI `--overwrite`) to replace it, or choose another `output` name. |
| `render.no_clips` | 422 | 1 | The timeline has no clips to render. | Add clips first with `add_clip_to_timeline` (CLI `split-video compose add-clip NAME FILE`) or build the timeline with `apply_plan`. |
| `render.request_invalid` | 422 | 1 | A render request, or a batch of render requests, is malformed. | Send a non-empty list of at most 50 render objects, each with a `project` (a name `list_timelines` returns) and its own `output` name; the other options are those of `render_timeline`. |
| `render.size_invalid` | 422 | 1 | The output size is not usable. | Give both width and height (or a preset), as even whole numbers within limits, e.g. `1080x1920`. |
| `ffmpeg.missing` | 501 | 1 | ffmpeg or ffprobe was not found on PATH. | Install ffmpeg (`sudo apt install ffmpeg` or `brew install ffmpeg`) so `ffmpeg -version` works, or use the Docker image, which bundles it. |
| `ffmpeg.failed` | 500 | 1 | ffmpeg exited with an error while reading or writing media. | Read the ffmpeg output in the message; check the source plays (`ffprobe FILE`) and the disk has room, then retry. Do not retry blindly: the same input fails the same way. |
| `extra.transcription_missing` | 501 | 4 | Transcription needs the optional faster-whisper package, which is not installed. | Install it with `uv sync --extra transcribe` (a source checkout) or `pip install 'split-video[transcribe]'`, or use the Docker image built with `--build-arg WITH_TRANSCRIPTION=1`. |
| `extra.ocr_missing` | 501 | 4 | On-screen text needs the Tesseract OCR program, which is not installed. | Install it with `sudo apt install tesseract-ocr tesseract-ocr-eng` or `brew install tesseract`, or use the Docker image built with `--build-arg WITH_OCR=1`. |
| `model.missing` | 501 | 1 | The audio classification model file is not installed. | Run `make fetch-model` (source checkout) or use the Docker image, which fetches the model at build time. |
| `transcript.missing` | 404 | 1 | The asset has no transcript yet (never made, or the file changed since). | Make one with `transcribe_asset` (CLI `split-video transcribe TARGET`, HTTP POST /api/v1/assets/{id}/transcribe), then ask again. |
| `ocr.missing` | 404 | 1 | The asset has no on-screen text yet (never made, or the file changed since). | Make it with `ocr_asset` (CLI `split-video ocr TARGET`, HTTP POST /api/v1/assets/{id}/ocr), then ask again. |
| `analysis.missing` | 404 | 1 | The asset has nothing to search or score yet (no transcript, on-screen text or scene signals). | Run the step the message names first: `transcribe_asset`, `ocr_asset` or `analyze_scenes` (each also a CLI command and an HTTP POST under /api/v1/assets/{id}/). |
| `job.unknown` | 404 | 1 | No job or render batch with that id is known to this server. | List what exists with `render_queue_status` (CLI `split-video compose render-queue`, HTTP GET /api/v1/jobs); ids do not survive a server restart. |
| `job.failed` | 500 | 1 | The background job failed. | Read the message for the cause, fix it, and submit the job again. |
| `plan.invalid` | 422 | 1 | The edit plan has problems; nothing was written. | Fix every entry of `errors` (each has a `path` into the plan, a `code` and a `hint`) and send the plan again; `dry_run` validates without writing. The codes are in docs/edit-plan.md. |
| `ops.invalid` | 422 | 1 | The edit-op batch has problems; nothing was written. | Fix every entry of `errors` (each has a `path` like /ops/2/clip, a `code` and a `hint`) and send the batch again; `dry_run` checks it without writing. The codes are in docs/edit-ops.md. |
| `request.invalid` | 422 | 1 | An argument is missing, of the wrong type or outside its allowed values. | Compare the arguments with the tool's input schema (MCP), `--help` (CLI) or openapi.json (HTTP) and send corrected values. |
| `error.internal` | 500 | 1 | An unexpected failure inside the engine. | Retry once. If it fails again, report it: send the message and the request that caused it, since it is a bug rather than a mistake in the request. |
