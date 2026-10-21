# Edit plans: build a whole timeline from one JSON document

An agent that wants a highlights reel should not make fifty single edits. It writes one **plan** (sources, in/out,
order, transitions, titles, tracks) and `apply_plan` creates or replaces the timeline project with it. The plan is the
central contract between an agent and the editor, so it is versioned, strict and published as a JSON Schema.

- **Atomic.** The whole project is built in memory and saved with one `ProjectStore.save_timeline`. A plan with any
  problem creates and changes nothing, and a failed save leaves the previous project as it was.
- **Every error at once.** An invalid plan is answered with *all* its problems (`path`, stable `code`, `message`,
  `hint`), not the first one, so an agent fixes the document in one round trip.
- **Strict.** Unknown fields are errors that name the field and list the allowed ones.
- **Same JSON everywhere.** HTTP, CLI and MCP return identical documents.

| Surface | Call |
| --- | --- |
| HTTP | `PUT /api/v1/compose/projects/{name}/plan[?dry_run=true]`, body = the plan, optional `If-Match: "<revision>"`. `GET /api/v1/schemas/edit-plan` serves the schema. |
| CLI | `split-video apply-plan <plan.json or -> [--root DIR] [--base-revision N] [--dry-run] [--json]` (`-` reads stdin). `split-video plan-schema` prints the schema. |
| MCP | `apply_plan(plan, dry_run, base_revision)`, `get_edit_plan_schema()` |
| Python | `edit_plan.validate_plan(media, plan, store=None) -> PlanReport`, `edit_plan.build_timeline(media, plan) -> Project`, `edit_plan.apply_plan(media, store, plan, dry_run=False, base_revision=None) -> dict`, `edit_plan.migrate_plan(doc)`, `edit_plan.highlights_to_plan(result, project=...)` |

The engine is `src/split_video/edit_plan.py`. The schema is generated from the same field tables the validator reads and
committed as [`docs/schemas/edit-plan.v1.schema.json`](schemas/edit-plan.v1.schema.json) (JSON Schema draft 2020-12;
`make schemas` regenerates it, CI and a test check that it is current).

In the HTTP route the URL names the project: `project` may be left out of the body, and a different one is a
`plan.project.mismatch` error. The revision may be sent as `If-Match` (HTTP), `--base-revision` (CLI), `base_revision`
(MCP) or inside the plan; if two of them are given they must agree (`plan.base_revision.conflict`).

## The format (`schema_version` 1)

```json
{
  "schema_version": 1,
  "project": "best-of",
  "mode": "create",
  "tracks": [
    {"kind": "video", "clips": [{"asset": "a_0123456789abcdef", "in": 12.5, "out": 41}]}
  ]
}
```

### Plan

| Field | Type | |
| --- | --- | --- |
| `schema_version` | integer, required | `1`. See [versioning](#versioning). |
| `project` | string, required | Name of the timeline project (no `/`, not starting with `.`). |
| `mode` | `"create"` (default) or `"replace"` | `create` fails if the project exists. `replace` rewrites an existing project. |
| `base_revision` | integer or null | Mode `replace`: the revision the plan was written against (from `show_timeline` / `compose show`). If the project has moved on: HTTP 409, CLI exit 3, MCP `revision_conflict`, nothing written. Not allowed in `create`. |
| `replace_unconditionally` | boolean, default false | Mode `replace` only: overwrite whatever revision is stored, losing any edit made since you looked. The explicit, loudly named alternative to `base_revision`. Giving both is `plan.replace.ambiguous`. |
| `tracks` | array, required | 1 to 16 tracks, bottom to top: a later video track draws over an earlier one. At least one must be a video track. |
| `meta` | object | Free-form notes for yourself. Echoed in the response, never stored, never validated, not part of `plan_hash`. |

**Replace rule.** Replacing an existing project must carry `base_revision` (409 on mismatch, like every other write) or
`"replace_unconditionally": true`. Replacing a project that does not exist is `plan.project.not_found`; use `create`.

### Track

| Field | Type | |
| --- | --- | --- |
| `id` | string | Optional. Generated deterministically from the plan when absent. |
| `kind` | `"video"` or `"audio"`, required | |
| `magnetic` | boolean | A magnetic track keeps its clips back to back in list order (the main track) and supports crossfades. Other tracks place clips with `start` and may not overlap. Default: true for the first video track, false for every other track. |
| `clips` | array | The clips, in list order. An empty or missing list is only a warning. |

### Clip

Field names are the `Clip` model's, with `in` / `out` as the one spelling of the source range (no aliases).

| Field | Type | |
| --- | --- | --- |
| `id` | string | Optional; generated when absent. Unique among clips. Letters, digits, `_`, `-`, `.`, at most 64 characters. |
| `kind` | `"media"` (default) or `"text"` | |
| `asset` | string | Media clips: an asset id (`a_` + 16 hex digits, from `list_assets`). Preferred. |
| `source` | string | Media clips: a path relative to the workspace root that is *already an asset* (`scan_library` registers files). Used only when `asset` is absent; with both and they disagree, the asset wins and there is a `plan.clip.source_mismatch` warning. One of the two is required. |
| `in` | number, default 0 | Seconds into the source where the clip starts. |
| `out` | number | Seconds into the source where it ends. Default for media: the end of the asset. **Required for text clips**, where `out - in` is how long the title shows. |
| `start` | number | Where the clip begins on the timeline. Only on non-magnetic tracks (an error on a magnetic one). Default: right after the previous clip on the track, so a track of clips without `start` is laid end to end. |
| `transition` | `"cut"` (default) or `"crossfade"` | How the clip joins the one before it. Crossfades only on magnetic tracks and never on the first clip. |
| `transition_duration` | number, default 0 | Crossfade seconds; required above 0 with `crossfade`. Must fit in the room the neighbouring clips leave. |
| `gain` | number 0..4, default 1 | Volume multiplier. |
| `muted` | boolean | |
| `x`, `y` | number 0..1, default 0.5 (text: `y` 0.85) | Overlay centre as a fraction of the frame. |
| `scale` | number 0.05..4, default 1 | Overlay width as a fraction of the frame width. |
| `opacity` | number 0..1, default 1 | |
| `brightness` | number -1..1, default 0 | ffmpeg `eq` semantics. |
| `contrast`, `saturation` | number 0..3, default 1 | |
| `crop` | object `{"x","y","w","h"}` | Media clips on video tracks: the part of the source frame to show, as fractions of the frame, origin top-left. Absent = the whole frame. `x`, `y` in 0..1, `w`, `h` from 0.02 to 1, `x + w` and `y + h` at most 1 (an error `plan.clip.crop_invalid` otherwise). Applied before the clip is scaled to fit the output frame, so for a vertical short compute it with the `crop_for_aspect` helper (CLI `compose crop --aspect 9:16`, MCP `set_clip_crop`, `GET /api/v1/assets/{id}/reframe?aspect=9:16`) instead of by hand, and render at the same aspect (`render --size 1080x1920`) so it fills the frame. |
| `text` | string, at most 1000 characters | Text clips only, required, not blank. |
| `font_size` | number 0.01..0.5, default 0.08 | Text clips only: fraction of the frame height. |
| `color` | `#rrggbb`, default `#ffffff` | Text clips only. |

Rules that need the project or the asset table (and so are not in the JSON Schema, only reported with codes): the asset
exists; an audio-only asset cannot go on a video track and a silent one not on an audio track; `in` < `out` <= the
asset's duration and at least 0.1 s long; clips on a non-magnetic track do not overlap; text goes on a video track that
is not magnetic; at most 2000 clips in the whole plan. The numeric limits and the crossfade rule are the editor's own
(`timeline_ops`), the same ones single edits obey.

### Response

```json
{
  "project": "best-of",
  "revision": 1,
  "action": "created",
  "dry_run": false,
  "clips": 5,
  "duration": 7.5,
  "tracks": [
    {"id": "9f2c1b7e", "kind": "video", "clips": 3, "end": 7.5},
    {"id": "4be0a1d3", "kind": "video", "clips": 1, "end": 3.0},
    {"id": "c7a815f2", "kind": "audio", "clips": 1, "end": 5.0}
  ],
  "warnings": [],
  "plan_hash": "5f0c...e1",
  "meta": {"by": "agent"}
}
```

`action` is `created` or `replaced`. `plan_hash` is the sha256 of the canonical plan (after migration, keys sorted, no
spaces, without `meta`): the same plan always gives the same hash and the same generated ids, so applying a plan twice
is detectable. With `dry_run` nothing is written and `revision` is the one the write would produce (a prediction, not a
reservation); the rest of the document is exactly what the real call returns, plus `timeline`: the whole resulting
project (what `show_timeline` returns: tracks, clips with their asset, in/out, start, transition, text and
properties), so an agent can inspect it before committing. A real call omits `timeline` (it can be large).

### Errors

A plan with any problem answers **HTTP 422**, **CLI exit 1** (the report is printed as JSON on stdout, a summary on
stderr) or an **MCP tool error** whose text is the compact report:

```json
{
  "error": "plan_invalid",
  "detail": "the plan has 2 problems; nothing was written",
  "ok": false,
  "errors": [
    {
      "path": "/tracks/0/clips/3/out",
      "code": "plan.clip.out_of_range",
      "message": "out point 400s is past the end of the source (300s)",
      "hint": "the asset is 300s long"
    },
    {
      "path": "/tracks/1/clips/0/asset",
      "code": "plan.asset.unknown",
      "message": "unknown asset 'a_0000000000000000'",
      "hint": "list_assets shows the known assets; scan_library registers new files"
    }
  ],
  "warnings": [],
  "plan_hash": "5f0c...e1"
}
```

`path` is a JSON pointer into the plan you sent (`""` is the whole document). `code` is stable and namespaced; SPL-55
folds these into the global error catalogue, so codes are not renamed. A stale `base_revision` is not a plan error: it
is the usual **409** / exit code **3** / `revision_conflict` tool error with `current_revision`.

| Code | Meaning |
| --- | --- |
| `plan.document.not_object` | The plan is not a JSON object. |
| `plan.document.not_json` | The plan holds something that is not plain JSON data. |
| `plan.schema_version.missing` | No `schema_version`. |
| `plan.schema_version.invalid` | `schema_version` is not an integer. |
| `plan.schema_version.unsupported` | Newer than the server supports, or older than the oldest migration. Nothing else is judged. |
| `plan.schema_version.migration_failed` | A migration raised. |
| `plan.field.unknown` | Unknown field; the hint lists the allowed ones. |
| `plan.field.required` | A required field is missing. |
| `plan.field.type` | Wrong JSON type (also `NaN` / infinity). |
| `plan.field.value` | A field that must have one fixed value does not. |
| `plan.field.enum` | Not one of the allowed values (`mode`, track `kind`, clip `kind`, `transition`). |
| `plan.field.range` | A number outside its range (`gain`, `x`, `in` below 0, ...). |
| `plan.field.pattern` | `color` is not `#rrggbb`. |
| `plan.field.length` | `text` or an id is too long. |
| `plan.project.name_invalid` | Not a usable project name. |
| `plan.project.mismatch` | HTTP only: the body's `project` differs from the URL. |
| `plan.project.exists` | Mode `create` and the project exists. |
| `plan.project.not_found` | Mode `replace` and there is no such project. |
| `plan.mode.field_not_applicable` | `base_revision` or `replace_unconditionally` in mode `create`. |
| `plan.replace.needs_base_revision` | Replace without `base_revision` or `replace_unconditionally`. |
| `plan.replace.ambiguous` | Both `base_revision` and `replace_unconditionally`. |
| `plan.base_revision.conflict` | The request's revision and the plan's differ. |
| `plan.tracks.empty` / `plan.tracks.too_many` / `plan.tracks.no_video` | No tracks, more than 16, or no video track. |
| `plan.clips.too_many` | More than 2000 clips in total (clips are then not examined further). |
| `plan.id.duplicate` / `plan.id.invalid` | A repeated or malformed `id`. |
| `plan.clip.media_source_missing` | A media clip with neither `asset` nor `source`. |
| `plan.clip.field_not_applicable` | A text-only field on a media clip, or `asset` / `source` / `crop` on a text clip, or `crop` on an audio-track clip. |
| `plan.clip.crop_invalid` | `crop` outside the frame or smaller than 0.02 (see the `crop` row). |
| `plan.asset.invalid_id` / `plan.asset.unknown` | Not an asset id / not in the asset table. |
| `plan.source.unknown` / `plan.source.outside_root` | The path is not a known asset (hint: `scan_library`) / escapes the workspace. |
| `plan.clip.audio_only_on_video_track` / `plan.clip.no_audio_for_audio_track` | The asset's streams do not fit the track. |
| `plan.clip.in_after_out` / `plan.clip.out_of_range` / `plan.clip.too_short` / `plan.clip.in_negative` | The source range is not legal for the asset. |
| `plan.clip.start_on_magnetic_track` | `start` on a back-to-back track. |
| `plan.clip.overlap` | Overlaps an earlier clip on a non-magnetic track. |
| `plan.clip.crossfade_first` / `plan.clip.crossfade_on_free_track` | A crossfade on a first clip or on a non-magnetic track. |
| `plan.clip.transition_duration` / `plan.clip.transition_too_long` | Crossfade with no duration / longer than the room the neighbours leave. |
| `plan.clip.text_required` / `plan.clip.text_on_main_track` / `plan.clip.text_on_audio_track` | Blank text; titles belong on a non-magnetic video track. |
| `plan.internal` | The checks passed but the layout failed. A bug: report the plan. |

Warnings (never block): `plan.track.empty`, `plan.clip.source_mismatch`, `plan.clip.transition_duration_ignored`
(a duration on a cut), `plan.project.replaces_existing` (what the replace overwrites).

## Rendering is not part of the plan

A plan says *what to cut*: which clips, in which order, with which crop. *How to export it* (the size, frame rate and
encoder of an export preset such as `vertical-1080`, and whether a picture of another aspect is letterboxed or
cropped to fill the frame) is chosen when you render: `split-video compose render <project> --preset vertical-1080
[--fit crop] [--size WxH]`, the `preset`, `fit`, `width` and `height` fields of the render request, or the same
arguments of the MCP tool `render_timeline`. Precedence for the frame size: explicit size, then preset, then the first
clip's size. `split-video presets` / `GET /api/v1/presets` / `list_export_presets` list the presets. The same plan can
be rendered to several destinations without changing it.

Burned-in captions are a render option in the same way: `--captions <style>` (with `--caption-range`, ...), the
`captions` and `caption_*` fields of the render request and the `captions` argument of `render_timeline`. They are
derived at render time from the assets' transcripts, so they are not in the plan or the project file, and the same plan
can be rendered with or without them. `split-video caption-styles` / `GET /api/v1/caption-styles` / `list_caption_styles`
list the styles.

## Examples

**Minimal.** One clip, one track:

```json
{
  "schema_version": 1,
  "project": "one",
  "tracks": [{"kind": "video", "clips": [{"asset": "a_0123456789abcdef"}]}]
}
```

**Highlights reel**, from `suggest_highlights` candidates (whose `start` / `end` are asset seconds, so they are `in` /
`out`), with crossfades, a title and a music bed. Asset ids are made up here; use yours from `list_assets`:

```json
{
  "schema_version": 1,
  "project": "stream-highlights",
  "tracks": [
    {
      "kind": "video",
      "clips": [
        {"asset": "a_0123456789abcdef", "in": 2000.0, "out": 2040.0},
        {"asset": "a_0123456789abcdef", "in": 5230.5, "out": 5262.0, "transition": "crossfade", "transition_duration": 1.0},
        {"asset": "a_0123456789abcdef", "in": 9100.0, "out": 9130.0, "transition": "crossfade", "transition_duration": 1.0}
      ]
    },
    {
      "kind": "video",
      "magnetic": false,
      "clips": [
        {"kind": "text", "text": "The deploy that failed", "start": 0, "out": 3, "font_size": 0.07},
        {"kind": "text", "text": "Fixed it live", "start": 38, "out": 41, "color": "#ffcc00"}
      ]
    },
    {
      "kind": "audio",
      "magnetic": false,
      "clips": [{"asset": "a_fedcba9876543210", "in": 0, "out": 100, "gain": 0.3}]
    }
  ],
  "meta": {"note": "three best moments, 1 s crossfades"}
}
```

In Python the first track comes from `edit_plan.highlights_to_plan(suggest_highlights(...), project="stream-highlights",
crossfade=1.0)`. From a shell, `jq` does the same job:

```sh
split-video highlights stream.mp4 --json --root /data \
  | jq '{schema_version: 1, project: "best-of",
         tracks: [{kind: "video",
                   clips: [.asset as $a | .candidates | sort_by(.start)[] | {asset: $a, in: .start, out: .end}]}]}' \
  | split-video apply-plan - --root /data
```

**Vertical short skeleton.** A main clip cropped to the centre 9:16 of a 1920x1080 recording (compute `crop`
with `crop_for_aspect`; here `w` = 9/16 * 1080 / 1920), a picture-in-picture reaction cam and a caption. Render it
with `split-video compose render short-001 --size 1080x1920`:

```json
{
  "schema_version": 1,
  "project": "short-001",
  "mode": "replace",
  "base_revision": 4,
  "tracks": [
    {"kind": "video", "clips": [{"asset": "a_0123456789abcdef", "in": 3600, "out": 3630, "crop": {"x": 0.342188, "y": 0, "w": 0.316406, "h": 1}}]},
    {
      "kind": "video",
      "magnetic": false,
      "clips": [
        {"asset": "a_89abcdef01234567", "in": 3600, "out": 3630, "start": 0, "scale": 0.3, "x": 0.8, "y": 0.18, "muted": true},
        {"kind": "text", "text": "Wait for it", "start": 1, "out": 4, "y": 0.8, "color": "#ffffff"}
      ]
    }
  ]
}
```

## Versioning

`schema_version` is an integer, and the schema file is named after it (`edit-plan.v1.schema.json`).

- **Additive, optional fields keep the version.** A plan that was valid stays valid; new fields (`crop` was added this way)
  appear in the schema and the field table with their defaults.
- **Any breaking change bumps the version**: removing or renaming a field, changing a default or a meaning, making an
  optional field required, tightening a range.
- **The server rejects a `schema_version` newer than it supports** (`plan.schema_version.unsupported`, and no other
  problem is reported: it cannot judge a format it does not know).
- **Older versions are accepted through `edit_plan.migrate_plan(doc)`**, which runs `MIGRATIONS[n]` (version n to n+1)
  until the document is current, before validation and hashing. There is nothing older than version 1 yet; the table is
  the seam version 2 will use, and a test drives it with a made-up version 0.
- Unknown fields stay errors within a version: the contract is the schema, not "whatever the server happens to ignore".
- Error codes are never renamed or reused for something else within a version.

## Limits

2000 clips in total and 16 tracks per plan; a clip is at least 0.1 s; text at most 1000 characters. A 300 clip plan
validates and applies in about 20 ms.
