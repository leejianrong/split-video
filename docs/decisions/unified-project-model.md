# Unified project model: the timeline project holds the splits (SPL-116)

Implements [ADR 0003](../adr/0003-one-editor-one-project-model.md) in the engine. No UI change: the split
editor frontend, the CLI split commands, the MCP split tools and the `/api/v1` split endpoints keep their names,
arguments, errors and output shapes; they now read and write a timeline project.

## What was there

- Split project: `<video>.split-video-project.json`, format version 1 (`start`/`end` per segment) or 2
  (adds `label`, `color`, `included`, `export_name`), plus `revision` and `source_file`. Written only through
  `ProjectStore.save_split/update_split`. The file holds no export settings and no analysis references: export
  options are per call, and analysis lives in `.split-video-cache/` keyed by asset id, so there is nothing else to carry.
- Timeline project: `<name>.split-video-timeline.json` (tracks, clips), format version 2.

## Data mapping

| Split project | Timeline project |
| --- | --- |
| the video | one video track (first, magnetic, "main"); every clip's `source` is the video's media id, `asset` its asset id |
| `segments[i]` | `clips[i]` of the main track, in order |
| `start`, `end` | `in`, `out` (source seconds, stored unrounded, so gaps and overlaps survive); `start` on the track is laid out back to back |
| `label` | clip `label` (new, optional, default `""`) |
| `color` | clip `label_color` (new, optional, default none; `color` stays the text-clip colour) |
| `included` | clip `included` (new, optional, default true) |
| `export_name` | clip `export_name` (new, optional, default none) |
| `source_file` | not stored: the clips' `source`/`asset` say it |
| `revision` | the timeline file's `revision` (see below) |
| `version` | the timeline file's `version` (stays 2, see below) |

There is no "note" field in the split project, so none is added.

Split numbers stay 1-based positions on the main track; split `n` is the boundary between clips `n` and `n+1`.
The split view of a timeline is its main track's media clips in order. Clip ids are kept when a segment keeps its
exact `(in, out)`; a segment that changed inherits the other clip properties (gain, crop, ...) of the old clip it
overlaps most, as a cut, and gets a new id if the old id is taken.

## Schema change (clip)

Four optional clip keys: `label`, `label_color`, `included`, `export_name`. They are written only when they
differ from the default and read with the default when absent, like `crop` (SPL-51). The timeline format version
stays 2: a file without them loads as before and an older build ignores the keys, so there is nothing to migrate in
either direction. The version bump the ADR asks for applies to the split file, which is migrated (below).
Rendering a timeline ignores `included` (it describes "export each segment as its own file"); a full render
still plays every clip on the track.

## Where the file lives

A video's timeline is `<video file name>.split-video-timeline.json` next to the video (so the sidecar rule holds
and in a subfolder it sits in that subfolder). For a video in the workspace root this is also the timeline named
`<video file name>` that the compose endpoints can open; `list_timelines` hides such files (a sibling file of that name exists) so the
old compose page's project list and the event stream do not change. `ProjectStore` gets two methods for it,
`load_video_timeline` and `update_video_timeline`, keyed by the video's id like the split methods were.
`InMemoryProjectStore` keeps them in a separate dict.

## Revision mapping

One number. A video's revision is the revision of its unified file; while that file does not exist it is the
legacy split file's revision (0 if neither exists). The first save therefore continues the numbering: a legacy
project at revision 5 reads as 5, the first edit writes the timeline file as 6. `If-Match` / `--base-revision` /
`base_revision` compare against that one number, so a stale write is a 409 exactly where it was. `peek_revisions`
reports it as `("split", video_id)` and writes announce `RevisionChange("split", ...)`, so the event stream is
unchanged. A legacy `save_split` after the unified file exists would bump the old file only and is ignored; the
engine never calls it (kept in the store for tests and to author legacy files).

## Migration

1. Open (`segments`, `GET /state`, `list_segments`, an edit): `ProjectStore.load_video_timeline` finds no unified
   file, so the engine reads the legacy file and builds the timeline in memory (`split_model.from_legacy`): same
   segments, labels, colours, flags, names, and the legacy revision. Nothing is written.
2. The first save (any edit or `save_segments`) writes `<video>.split-video-timeline.json` with revision legacy+1.
   The legacy file is not modified or deleted, ever; once the unified file exists it wins.
3. A legacy file that cannot be parsed counts as nothing saved, as before.
4. A video with neither file opens as the video's whole duration, one clip, unsaved (`saved: false`), as before.

## Adapter strategy

`project.py` stays the one place the rules live. Its functions that took or returned `list[SavedSegment]` keep their
signatures (plus an optional `media` so the clips get asset ids); inside, they map through `split_model`
(`to_segments`, `apply_segments`, `from_legacy`) and `ProjectStore.update_video_timeline`. `editor/app.py`
stops calling `load_split_versioned`/`save_split` and goes through `project.py`. CLI and MCP already do.
Other tracks of the timeline (overlays, audio) are left alone by a split edit. If the main track holds anything but
media clips of this video (a compose edit mixed other sources or text in), a split write is refused with a
`ProjectError` that says to edit it in the compose editor; reading still works.

## Pinned contract

`tests/integration/test_split_contract.py` replays scripted sessions on the CLI (`--json` and table), the MCP tools and
the HTTP routes against golden outputs recorded on the pre-change code
(`tests/fixtures/split_contract/golden.json`), on fresh videos and on real legacy split files
(`tests/fixtures/legacy_split/`, versions 1 and 2).

## Risks and what stays legacy

- `ProjectStore` grows two methods; `load_split`, `load_split_versioned`, `save_split` and `update_split` remain for
  reading legacy files (migration) and for tests; no engine path writes the legacy file any more.
- Editing the same video through compose and the split adapters at once is coherent (one file, one revision), but
  a compose edit that adds other sources to the main track blocks split writes (see above).
- First split edit now registers the video in `.split-video-assets.json` (clips carry an asset id).
- Excluded segments still render in a full timeline render.
- Not changed: `*.split-video-project.json` is still read, never written or removed; the UI still has two pages;
  the compose API does not expose the new clip keys yet.
