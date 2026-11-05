# Revisions: editing the same project from two places



Every project (a video's splits, which are its timeline project since SPL-116, and compose timelines) carries a revision
number, stored in its file next to `version`. A project that was never saved,
or whose file predates revisions, is revision 0; each save adds 1. The
editor, `show`/`segments` output, `--json`, MCP results and API responses all
report it.

Writes can say which revision they are based on (`base_revision`). If the
project has changed since, the write is refused and nothing is saved:

- **CLI**: `--base-revision N` on every command that edits a project. A stale
  write exits with code 3 and tells you the current revision and how to retry
  (run `split-video compose show NAME` or `split-video segments VIDEO`, re-apply
  your change, retry with `--base-revision <current>`); with `--json` the
  error is also printed as `{"error": "revision_conflict", "current_revision": ...}`.
- **MCP**: every editing tool takes an optional `base_revision`; a stale write is
  an error whose text carries `{"error": "revision_conflict", "current_revision": N, ...}`
  and retry guidance.
- **HTTP**: send `If-Match: "N"` on any write (bodies and `DELETE` alike). A stale
  write gets `409` with `{"detail": ..., "error": "revision_conflict",
  "current_revision": N, "base_revision": M}`. Other 409s (output exists, project
  exists) have no `error` field.
- **The editors** send the revision they last saw with every save. The visual editor (`/app/`) also listens to
  `GET /api/v1/events`: when the CLI, MCP or another tab saves the open project, it loads the new version within about
  a second (the watcher polls once a second), keeps the selected clips that still exist and says so in a notice ("The
  project was changed by another tool", plus "The clip you selected was removed" when that applies). The playhead is
  clamped to the new length. If a save of the editor itself loses the race (409), the latest version is loaded and a
  prompt offers **Apply my change again**, which sends the same change on top of it (nothing is re-applied without
  that click). The older split editor reloads and shows "Project changed elsewhere; reloaded the latest version,
  please redo your last change."

**`base_revision` is optional by design.** Leave it out and the edit is applied
to the latest version: the load, change and save happen under a lock, so
concurrent edits from the CLI, MCP and the editor never lose each other's
changes, but nothing checks that you were looking at the latest. Pass it when
your change only makes sense on the version you saw (an agent that read the
project, reasoned about it, then edits by clip number, for example).

The lock is an advisory `flock` on an empty `<project file>.lock` file in a hidden
`.split-video-locks/` folder next to each project file (so it lives in the bind-mounted video folder under
Docker without cluttering it; it is git-ignored and safe to delete when nothing is running). It
works across processes on the same machine, and across containers sharing a
bind mount on a Linux host. It does not work where `flock` does not (Windows,
some network filesystems such as NFS without lock support, and some
Docker Desktop file shares): there writers in different processes can race;
the revision check still catches stale writers that pass `base_revision`, but
two simultaneous writes to the same file could drop one.

A split project migrated from a legacy `.split-video-project.json` keeps counting from the
legacy file's revision: it reads as N before the first save and the first save writes N+1.

## Undo and redo

Every project (a video's timeline and a named one) has a linear edit history, kept by the engine, so the editor, the
CLI, MCP and the HTTP API share it. Each write is one entry: a batch of ops, or the editor's split of several clips, is
one undo. The history is not part of the project file (its format does not change): it is a sidecar,
`.split-video-history/<project file name>.json` next to the project, written under the same lock as the project, so it
survives a reload and, under Docker, lives in the video folder. It is git-ignored and safe to delete (the project
then has nothing to undo).

**Undo is a forward write.** It saves the state before the latest edit as a NEW revision, so revisions only go up and
every client, the events stream, stills and the playback cache treat it like any other edit. Redo does the same in the
other direction; a new edit clears what could be redone (no branching). `base_revision` works as on any write.

- **HTTP**: `POST /compose/projects/{name}/undo` and `/redo`, and `POST /project/undo?path=` and `/redo` for a
  recording's timeline (`If-Match` as everywhere). Every project view (`GET`, any edit) carries
  `history: {can_undo, can_redo, undo_label, redo_label}`.
- **CLI**: `split-video compose undo NAME [--base-revision N]` and `compose redo NAME`; `compose show` (also with
  `--video`) and every edit's `--json` carry the same `history`.
- **MCP**: tools `undo` and `redo` (`name` or `video`, optional `base_revision`); `show_timeline` and every timeline
  result carry `history`.
- Nothing to undo or redo is the error `history.empty` (HTTP 422, CLI exit 1); `can_undo` / `can_redo` say so in advance.
  `undo_label` names what the next undo reverts ("Split clip"); a batch of mixed ops is "N edits".

**Other writers.** The history is per project and linear, and the CLI, MCP and other tabs record into the same one, so
undo reverses the latest write whoever made it (the editor then shows the usual "changed by another tool" notice).
Pass `base_revision` to refuse instead when someone has edited since you looked: a stale undo is a `revision_conflict`
like any other write. A project changed by something that does not record history (a hand-edited file, an older
version) has no usable history: `can_undo` is false until the next edit, which starts a new one.

**Limits.** The latest 100 entries, within 4 MiB of stored project states; the oldest are dropped first. A video with
nothing saved yet is its recording as one clip, and undoing its first edit goes back to that.

