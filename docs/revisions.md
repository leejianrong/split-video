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
- **The editors** send the revision they last saw with every save. If something
  else changed the project meanwhile, the page reloads the latest version and
  shows "Project changed elsewhere; reloaded the latest version, please redo
  your last change."

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
