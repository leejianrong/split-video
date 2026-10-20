# Edit operations: batch edits with dry-run

An agent that wants to *change* a timeline (trim three clips, add a title, move a track) should not make a round trip
per edit, and should be able to try the edits before committing them. It sends one **ops document**: a list of the same
single edits the editor, the CLI and MCP already have, applied in order and **atomically**. `dry_run` runs the identical
engine and answers with the resulting project and any errors, writing nothing.

The ops are the incremental sibling of the [edit plan](edit-plan.md), and the two are one design: the same error
report, the same revision rules, the same schema publishing and versioning.

| | Edit plan (`apply_plan`) | Edit ops (`apply_ops`) |
| --- | --- | --- |
| What it is | The whole project: creates it, or replaces it wholesale | A list of incremental edits to a project that exists |
| Needs | `mode` `create`, or `replace` + `base_revision` / `replace_unconditionally` | The project exists; `base_revision` optional |
| Addresses clips by | Nothing: it describes the final result | Id (existing, or `id` you gave an earlier op) |
| Errors | `plan.*`, all at once | `ops.*` (and shared `plan.*`), shape problems all at once, then the first op the project refuses |
| Use it for | A highlights reel from candidates, a fresh short | "Trim clip 3, fade into it, add a caption" on what is already there |

- **Atomic.** The ops run in order on an in-memory `Project` (the pure functions of `timeline_ops`, the rules every single
  edit obeys) and the result is saved with ONE `ProjectStore.update_timeline`, under the store's write lock and the
  caller's `base_revision`. If any op fails, nothing is saved: the project's content and revision are exactly as before.
- **Dry run never writes.** Not the project, not the asset table: a dry run only reads assets (`assets.peek`) and never
  registers, re-hashes or relinks a file. It is tested with a store spy (zero save/create/update calls) and by comparing
  the stored bytes and revision before and after, on the in-memory and the local store.
- **Strict.** Unknown ops and fields are errors that list what is allowed.
- **Same JSON everywhere.** HTTP, CLI and MCP return identical documents.

| Surface | Call |
| --- | --- |
| HTTP | `POST /api/v1/compose/projects/{name}/ops[?dry_run=true]`, body = the ops document, optional `If-Match: "<revision>"`. `GET /api/v1/schemas/edit-ops` serves the schema. |
| CLI | `split-video apply-ops <ops.json or -> [--root DIR] [--base-revision N] [--dry-run] [--continue-on-error] [--json]`. `split-video ops-schema` prints the schema. |
| MCP | `apply_ops(ops, dry_run, base_revision, continue_on_error)` (`ops` is the whole document), `get_edit_ops_schema()` |
| Python | `edit_ops.apply_ops(media, store, doc, dry_run=False, base_revision=None, continue_on_error=False) -> dict`, `edit_ops.validate_ops(...) -> OpsReport` (a dry run's report, valid or not), `edit_ops.migrate_ops(doc)` |

The engine is `src/split_video/edit_ops.py`. The schema is generated from the `OPS` table in that file, the one the
validator reads, and committed as [`docs/schemas/edit-ops.v1.schema.json`](schemas/edit-ops.v1.schema.json) (JSON Schema
draft 2020-12). `make schemas` regenerates it (and the plan's); CI and a test check that both are current.

In the HTTP route the URL names the project: `project` may be left out of the body, and a different one is
`ops.project.mismatch`. The revision may be sent as `If-Match` (HTTP), `--base-revision` (CLI), `base_revision` (MCP) or in
the document; if two are given they must agree (`ops.base_revision.conflict`). `continue_on_error` may be the document's
field or the flag / argument; either one switches it on.

## The format (`schema_version` 1)

```json
{
  "schema_version": 1,
  "project": "best-of",
  "base_revision": 7,
  "ops": [
    {"op": "trim_clip", "clip": "3f9a1c02", "in": 12.5, "out": 41},
    {"op": "add_track", "id": "titles", "kind": "video"},
    {"op": "add_text_clip", "id": "cap1", "track": "titles", "text": "The deploy that failed", "duration": 3},
    {"op": "set_text_properties", "clip": "cap1", "color": "#ffcc00"}
  ]
}
```

### Document

| Field | Type | |
| --- | --- | --- |
| `schema_version` | integer, required | `1`. See [versioning](#versioning). |
| `project` | string, required | Name of the **existing** timeline project (a missing one is the usual not-found error, not a batch error). |
| `base_revision` | integer or null | The revision the ops were written against (from `show_timeline` / `compose show`). If the project has moved on: HTTP 409, CLI exit 3, MCP `revision_conflict`, nothing written. Absent: apply to the latest version. |
| `continue_on_error` | boolean, default false | **Dry run only.** Keep going after a failing op and report every independent problem. Asking for it on a real run is `ops.continue_on_error.needs_dry_run`: a batch with a problem is never written. |
| `ops` | array, required | 1 to 1000 operations, applied in order. |
| `meta` | object | Free-form notes for yourself. Echoed in the response, never stored or validated, not part of `ops_hash`. |

### Addressing clips and tracks: caller-chosen ids

Clips and tracks are addressed by **id** (the ids `show_timeline` lists). Operations that create something (`add_clip`,
`add_text_clip`, `add_track`) take an optional `id`; later ops in the same batch use it as `clip` or `track`. That is
the one mechanism for referring to something the batch itself creates:

```json
{"op": "add_clip", "id": "intro", "asset": "a_0123456789abcdef", "out": 10},
{"op": "set_transition", "clip": "intro", "transition": "crossfade", "duration": 1}
```

Without an `id` the engine generates one, deterministically from the batch's `ops_hash` and the op's position (8 hex
digits, avoiding every id in use), so a dry run predicts exactly the ids the real run will give; it is reported in the
op's `results[].ids`, but no op in the batch can refer to it, so name anything you will need again. An `id` is letters,
digits, `_`, `-`, `.`, at most 64 characters; clip ids and track ids are separate namespaces; an id the project already
uses is `ops.id.duplicate`. Positions (`index`, `to_index`) are **zero-based**.

### Operations

Every operation maps 1:1 to a single edit (`timeline_service.<op>` over `timeline_ops.<op>`), and obeys the same rules.
All fields below are exactly the schema's; `in` and `out` are seconds into the source and the numeric ranges are the
clip ranges of the [plan](edit-plan.md#clip).

| `op` | Fields | Does |
| --- | --- | --- |
| `add_clip` | `id`, `track`, `asset` or `source` (one required), `index`, `start`, `in`, `out` | Add a clip of an asset. `track` defaults to the main video track. On a magnetic track `index` inserts (default: the end) and `start` is an error; on other tracks `start` places (default: after the last clip) and `index` is an error. `out` defaults to the end of the asset. `source` must already be an asset (`scan_library`): a path is **never registered** by a batch, so a dry run can't write. |
| `trim_clip` | `clip`, `in`, `out` (at least one) | Change which part of the source the clip uses. On a free track, moving `in` keeps the content where it is in time. |
| `move_clip` | `clip`, `to_index` | Move a clip within its magnetic track. |
| `remove_clip` | `clip` | Remove a clip (the file is untouched). |
| `set_transition` | `clip`, `transition` (`cut`/`crossfade`), `duration` | Set how the clip joins the one before it. A request that cannot be honoured is an error, not clamped. |
| `set_clip_properties` | `clip`, any of `gain`, `muted`, `x`, `y`, `scale`, `opacity`, `brightness`, `contrast`, `saturation` (at least one) | Change volume, overlay placement or colour. Only the fields you give change. |
| `add_text_clip` | `id`, `track` (required), `text`, `duration` (both required), `start`, `font_size`, `color`, `x`, `y` | Add a title to a non-magnetic video track. |
| `set_text_properties` | `clip`, any of `text`, `font_size`, `color` (at least one) | Change a title. |
| `add_track` | `id`, `kind` (`video`/`audio`, required), `magnetic` | Append an empty track on top of the stack (free unless `magnetic`). |
| `remove_track` | `track` | Remove a track and its clips. The last video track stays. |
| `move_track` | `track`, `to_index` | Reorder tracks (later draws on top). |
| `place_clip` | `clip`, `start`, `track` | Put a clip at `start` on a free track, optionally moving it to another free track of the same kind. |

`render` and project creation are not edits and are not ops.

### Response

```json
{
  "project": "best-of",
  "revision": 8,
  "action": "edited",
  "dry_run": false,
  "applied": 4,
  "ops": 4,
  "clips": 6,
  "duration": 81.5,
  "tracks": [
    {"id": "9f2c1b7e", "kind": "video", "clips": 5, "end": 81.5},
    {"id": "titles", "kind": "video", "clips": 1, "end": 3.0}
  ],
  "results": [
    {"index": 0, "ok": true, "summary": "trimmed clip 3f9a1c02 to 12.5s..41s", "ids": {"clip": "3f9a1c02", "track": "9f2c1b7e"}},
    {"index": 1, "ok": true, "summary": "added video track titles", "ids": {"track": "titles"}},
    {"index": 2, "ok": true, "summary": "added title cap1 to track titles", "ids": {"clip": "cap1", "track": "titles"}},
    {"index": 3, "ok": true, "summary": "set color on title cap1", "ids": {"clip": "cap1", "track": "titles"}}
  ],
  "warnings": [],
  "ops_hash": "5f0c...e1"
}
```

It has the plan's result shape (`project`, `revision`, `action`, `dry_run`, `clips`, `duration`, `tracks`, `warnings`,
`meta`) with `ops_hash` in place of `plan_hash`. `results` has one entry per op: `ids` are the clip and track the op
touched (for a creating op, the id it made). `ops_hash` is the sha256 of the canonical document (after migration, keys
sorted, no spaces, without `meta`).

**Dry run.** `dry_run: true` returns the same document plus `timeline`: the whole resulting project, exactly what
`show_timeline` returns (tracks, clips with asset, in/out, start, transition, text and properties, `revision` being the
predicted one). The rest is exactly what the real call returns. `revision` is a **prediction** (the current revision plus
one), not a reservation: another writer can move the project before you apply. A plan's dry run carries the same
`timeline` key, so an agent reads both the same way.

### Errors

A batch with any problem answers **HTTP 422**, **CLI exit 1** (the report as JSON on stdout, a summary on stderr) or an
**MCP tool error** whose text is the compact report. It has the plan report's shape (`path`, `code`, `message`, `hint`)
and also says how far it got:

```json
{
  "error": "ops_invalid",
  "detail": "the batch has 1 problem; nothing was written",
  "ok": false,
  "errors": [
    {
      "path": "/ops/6/clip",
      "code": "ops.clip.not_found",
      "message": "no clip 'ghost'",
      "hint": "clip ids come from show_timeline, or from the `id` of an add_clip / add_text_clip earlier in this batch"
    }
  ],
  "warnings": [],
  "ops_hash": "5f0c...e1",
  "dry_run": false,
  "applied": 6,
  "results": [
    {"index": 0, "ok": true, "summary": "added clip n1 (a_1579d56d69a5329b 40s..50s) to track 9f2c1b7e", "ids": {"clip": "n1", "track": "9f2c1b7e"}},
    {"index": 6, "ok": false, "summary": "no clip 'ghost'", "ids": {}, "code": "ops.clip.not_found"}
  ]
}
```

`path` is a JSON pointer into the document you sent (`/ops/6/clip`: the `clip` field of the op at index 6). `applied` is
how many ops applied in memory before the batch stopped; **nothing was written** whatever its value. On a dry run the
report also has `timeline`: the project as it stands after the ops that did apply.

**Which problems are reported.**

1. **Shape problems are all reported together**, whatever their position: unknown op, unknown field, wrong type, value out
   of range, a missing required field, bad or repeated `id`s. They need no project, so an agent fixes them in one round
   trip. If there are any, the ops are not tried (`applied` 0), unless `continue_on_error` is on.
2. **Then the ops run in order and the first one the project refuses stops the batch.** Later ops usually depend on
   what the failed op would have done (an id it creates, the position of a clip), so going on would only produce
   misleading follow-on errors. This is the default and what a real run always does.
3. **`continue_on_error` (dry run only)** instead goes on and collects every independent problem. An op that refers to an
   id a failed (or skipped) op was to create is reported as `ops.skipped_dependency` and not tried. It is a diagnostic: an
   op on an existing clip that an earlier failed op was meant to change is still tried against the unchanged project.

A stale `base_revision` is not a batch error: it is the usual **409** / exit code **3** / `revision_conflict` tool error
with `current_revision`, and a missing project the usual not-found error.

Codes are stable and namespaced; SPL-55 folds them into the global error catalogue, so they are not renamed. Where the
problem is the same as one the plan reports, the `plan.*` code is reused (the [plan's catalogue](edit-plan.md#errors)
describes them); field-level problems use the `ops.field.*` family.

| Code | Meaning |
| --- | --- |
| `ops.document.not_object` | The document is not a JSON object. |
| `ops.document.not_json` | The document holds something that is not plain JSON data. |
| `ops.schema_version.missing` / `ops.schema_version.invalid` | No `schema_version` / not an integer. |
| `ops.schema_version.unsupported` | Newer than the server supports, or older than the oldest migration. Nothing else is judged. |
| `ops.schema_version.migration_failed` | A migration raised. |
| `ops.field.unknown` | Unknown field; the hint lists the allowed ones. |
| `ops.field.required` | A required field is missing. |
| `ops.field.type` | Wrong JSON type (also `NaN` / infinity; a boolean is not a number). |
| `ops.field.value` | A field that must have one fixed value does not. |
| `ops.field.enum` | Not one of the allowed values (`kind`, `transition`). |
| `ops.field.range` | A number outside its range (`gain`, `x`, `index` below 0, `duration` below 0.1 for a title, ...). |
| `ops.field.pattern` | `color` is not `#rrggbb`. |
| `ops.field.length` | `text` or an id is too long. |
| `ops.op.unknown` | `op` is not one of the twelve; the hint lists them. |
| `ops.op.no_changes` | A `trim_clip`, `set_clip_properties` or `set_text_properties` that gives nothing to change. |
| `ops.ops.empty` / `ops.ops.too_many` | No ops / more than 1000. |
| `ops.project.name_invalid` | Not a usable project name. |
| `ops.project.mismatch` | HTTP only: the body's `project` differs from the URL. |
| `ops.base_revision.conflict` | The request's revision and the document's differ. |
| `ops.continue_on_error.needs_dry_run` | `continue_on_error` without a dry run. |
| `ops.id.invalid` / `ops.id.duplicate` | A malformed `id`; one repeated in the batch or already used in the project. |
| `ops.clip.not_found` / `ops.track.not_found` | No clip / track has that id (yet: an id from a creating op only exists after it ran). |
| `ops.skipped_dependency` | `continue_on_error`: the op uses an id from a failed op, so it was not tried. |
| `ops.index.out_of_range` | `index` / `to_index` is outside the current list. |
| `ops.clip.index_on_free_track` | `index` on a track that places clips with `start`. |
| `ops.clip.move_on_free_track` | `move_clip` on a free track (use `place_clip`). |
| `ops.clip.start_negative` | A start before 0. |
| `ops.clip.not_text` | `set_text_properties` on a media clip. |
| `ops.clip.source_missing` | `trim_clip` on a clip whose asset is not in the asset table (its duration is unknown). |
| `ops.clip.track_kind_mismatch` / `ops.clip.dest_is_magnetic` | `place_clip` to a track of another kind / to the magnetic main track. |
| `ops.track.last_video` | `remove_track` of the only video track. |
| `ops.rule.violated` | A rule of the editor this table does not name yet (the message says which). Not expected. |
| `plan.asset.invalid_id` / `plan.asset.unknown` | `asset` is not an asset id / not in the asset table. |
| `plan.source.unknown` / `plan.source.outside_root` / `plan.clip.media_source_missing` | `source` is not a known asset (never registered by a batch) / escapes the workspace / neither `asset` nor `source`. |
| `plan.clip.audio_only_on_video_track` / `plan.clip.no_audio_for_audio_track` | The asset's streams do not fit the track. |
| `plan.clip.in_after_out` / `plan.clip.out_of_range` / `plan.clip.too_short` / `plan.clip.in_negative` | The source range (or a title's `duration`) is not legal. |
| `plan.clip.start_on_magnetic_track` | `start` on a back-to-back track. |
| `plan.clip.overlap` | The clip would overlap another on a free track. |
| `plan.clip.crossfade_first` / `plan.clip.crossfade_on_free_track` | A crossfade on a first clip / on a free track. |
| `plan.clip.transition_duration` / `plan.clip.transition_too_long` | Crossfade with no duration / longer than the room the neighbours leave. |
| `plan.clip.text_required` / `plan.clip.text_on_main_track` / `plan.clip.text_on_audio_track` | Blank text; titles belong on a non-magnetic video track. |

Warnings (never block): `plan.clip.transition_duration_ignored` (a `duration` on a `cut`).

## Examples

**Try, then commit.** Ask what a batch would do, read `timeline`, then send the same document without `dry_run`:

```sh
split-video apply-ops edit.json --root /data --dry-run --json | jq '.timeline.tracks[0].clips | length'
split-video apply-ops edit.json --root /data --base-revision 7
```

**Find every mistake at once** (dry run with `continue_on_error`; the second op depends on the failed first one):

```json
{
  "schema_version": 1, "project": "best-of", "continue_on_error": true,
  "ops": [
    {"op": "add_clip", "id": "c9", "asset": "a_0000000000000000"},
    {"op": "trim_clip", "clip": "c9", "out": 5},
    {"op": "remove_clip", "clip": "nope"}
  ]
}
```

answers `plan.asset.unknown` at `/ops/0/asset`, `ops.skipped_dependency` at `/ops/1` and `ops.clip.not_found` at
`/ops/2/clip`.

**A title over the best moment**, a new overlay track and a picture-in-picture, referring to what the batch creates:

```json
{
  "schema_version": 1, "project": "short-001", "base_revision": 4,
  "ops": [
    {"op": "add_track", "id": "ov", "kind": "video"},
    {"op": "add_clip", "id": "cam", "asset": "a_89abcdef01234567", "track": "ov", "in": 3600, "out": 3630, "start": 0},
    {"op": "set_clip_properties", "clip": "cam", "scale": 0.3, "x": 0.8, "y": 0.18, "muted": true},
    {"op": "add_text_clip", "id": "cap", "track": "ov", "text": "Wait for it", "duration": 3, "start": 1}
  ]
}
```

## Versioning

The policy is the plan's: `schema_version` is an integer and the schema file is named after it
(`edit-ops.v1.schema.json`).

- **Additive, optional fields keep the version** (and a new op is additive). A document that was valid stays valid.
- **Any breaking change bumps the version**: removing or renaming an op or field, changing a default or a meaning,
  making an optional field required, tightening a range.
- **The server rejects a `schema_version` newer than it supports** (`ops.schema_version.unsupported`, and nothing else is
  judged).
- **Older versions are accepted through `edit_ops.migrate_ops(doc)`**, which runs `MIGRATIONS[n]` (version n to n+1) until
  the document is current, before validation and hashing. There is nothing older than version 1 yet; a test drives the
  seam with a made-up version 0.
- Unknown ops and fields stay errors within a version.
- Error codes are never renamed or reused for something else within a version.

## Limits

1000 ops per document. A clip is at least 0.1 s; text at most 1000 characters. Every op is cheap (an in-memory edit): a
200-op batch dry-runs and applies in about 45 ms on either store.
