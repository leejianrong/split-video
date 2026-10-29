# HTTP API and API token

The editor's JSON API is versioned under `/api/v1` (media at
`/api/v1/media/source?path=...`). Its OpenAPI schema is committed as
[`openapi.json`](../openapi.json) at the repo root, and CI fails if it drifts
from the code. After changing a route or schema, regenerate it with
`make openapi`. The old unversioned paths (`/api/...`, `/media/source`,
`/media/file`) still work for one release as deprecated aliases (they send a
`Deprecation: true` header and are not in the schema).

Project state responses carry a `revision`, and writes take an optional
`If-Match` header; see [Revisions](revisions.md).

`GET /api/v1/events` is a server-sent-events stream of job progress and project
revision changes, including edits made by the CLI or MCP server in another
process (`curl -N localhost:8765/api/v1/events`). The polling endpoints keep
working; the frame format is in the route's description in the schema.

## API token (agents and hosted use)

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

## Errors

Every failure, on the CLI, the MCP server and the HTTP API, carries a stable `code`, a `message` and a `fix` that names
the next action (`split-video --json-errors ...` prints it as JSON). The codes are listed in [errors.md](errors.md).

## Split endpoints and the timeline project

`/open`, `/state` and `POST /project` keep their shapes and `If-Match` / `409`
behaviour. The segments are now the main track of the video's timeline project
(`<video>.split-video-timeline.json`); a legacy `<video>.split-video-project.json`
opens as the same segments and is migrated by the first save, which leaves the old file
in place. The `revision` is one number across both. See
[the design note](decisions/unified-project-model.md).

## Import, split jobs and a video's own timeline (the Media tab)

These are what the editor's Media tab calls; each is a plain `/api/v1` route that agents can use too (all new in v1, so
no deprecated alias).

| Route | What it does |
|---|---|
| `POST /assets/upload?name=&folder=` | The raw request body is a media file: it is written into the workspace through the `MediaStore`, probed and registered. `201` with the asset. A bad name or type is `400` / `415` (`upload.invalid_name`, `upload.unsupported_type`, `upload.empty`), an unreadable file `422` (`media.unreadable`); nothing is left on disk after a failure. A name that is taken becomes `gig (2).mp4`. Files already in the workspace need no upload: `POST /assets/register`. |
| `POST /assets/{id}/split` | Queues a `split` job: find the silent gaps (cached next to the video), cut there and save the pieces as the video's segments, replacing the saved ones. Body (all optional): `silence_threshold`, `min_silence_duration`, `min_song_length`, `padding`, `label_prefix` (`"Song"` labels the pieces Song 1, Song 2 ...). Progress is thousandths of the audio; result `{video, segments, revision, labels}`; cancel with `POST /jobs/{id}/cancel` (nothing is saved). `If-Match` makes a stale save a job error. CLI `propose --apply` and MCP `propose_splits` run the same operation inline. |
| `GET /project/timeline?path=` | The video's own timeline in the compose shape (tracks with their `locked`, `hidden` and `muted` flags, clips with `label`, `label_color` and `included`, `revision`). A video with nothing saved is its recording as one clip, revision 0; reading writes nothing. The compose routes address timelines by a plain name and cannot name a video in a folder; these take the video's path like `/state`. |
| `POST /project/clips?path=` | Add the library asset (`{"asset": "a_...", "index": null}`) to the main video track, at `index` (default: the end), and save. `If-Match` as everywhere. Audio-only assets are `422 media.audio_only`. After this the main track holds more than the recording's own clips, so splitting that video says `segments.main_track_edited` (edit it with the compose tools). |
| `POST /project/ops?path=&dry_run=` | Apply an [edit-ops document](edit-ops.md) (`trim_clip`, `move_clip`, `place_clip`, `add_clip`, `set_track_flags`, ...) to the video's own timeline, atomically, with the same engine, errors and result as `POST /compose/projects/{name}/ops`. `project` may be left out of the body (given, it must be the `path`). `If-Match` as everywhere (a stale one is `409`); a locked track is `422` with `ops.track.locked`. A video with nothing saved becomes its recording as one clip first. This is what the timeline lanes call for every trim, move, drop and lane toggle; the editor then reads `GET /project/timeline` again. CLI `split-video apply-ops OPS --video PATH` and MCP `apply_ops(..., video=PATH)` run the same operation; `compose show --video PATH` and MCP `show_timeline(video=PATH)` list the clip ids and revision to write against. |
| `POST /assets/{id}/waveform` | Queues a `waveform` job: one streamed decode of the audio (never all in memory), stored next to the recording. `422` for an asset without audio. |
| `GET /assets/{id}/waveform` | The stored waveform: `peaks` (min and max per bucket, 2000 buckets across the file) and `loudness` (RMS dB per `loudness_window` seconds, 1 s). `404` until the job has run (the message says to `POST`), and for an asset without audio. The timeline draws the audio lane from this; the browser never decodes audio. No CLI or MCP twin: `report --prepare` and `prepare_asset` store the same loudness curve (they do not make the peaks). |

There is no CLI or MCP command for `POST /project/clips` yet: the compose commands add a clip to a named timeline, which
cannot be a video in a folder.

