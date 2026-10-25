# 0002: Agent-first highlights editor, hosted-ready seams

Status: accepted (2026-10-19). Board card: SPL-30. Amends 0001.
Amended by [0003](0003-one-editor-one-project-model.md): one editor, one project model; the M3 frontend is one port, not two.
Frontend details (layout, build, serving, state, testing): [0004](0004-frontend-stack-and-structure.md).

## Context

Direction set on 2026-10-19: the product turns long recordings (hours of
stream, gig or lecture footage) into highlights and shorts. Agents are
first-class users next to humans: anything the UI can do, an agent can do
through a documented API. We stay local-first now, but a hosted service
must be possible later without a rewrite.

Today the editor holds one global session, clips reference files by path,
there is no API versioning, and long analysis runs inline in request
handlers. Each of those blocks a second user, a second client or a remote
server.

## Decision

- **Product focus: long recordings to highlights and shorts.** Import,
  analyse, suggest highlights, edit, reframe vertically, caption, export.
  Features that do not serve that path are cut or deferred (see M4).
- **Five seams, each an interface with a local implementation first:**
  - *Engine*: `project.py` stays the only place editing rules live. The web
    UI, CLI and MCP server remain thin adapters (as in 0001).
  - *API*: a versioned `/api/v1` with `openapi.json` committed and checked
    in CI. Project state is per project, not a global session. Projects
    carry a revision; stale writes get `409`. Errors are structured. SSE
    streams job progress and project changes.
  - *Storage*: `ProjectStore` and `MediaStore` interfaces, local-disk
    implementations. Clips reference content-derived asset ids, not paths.
  - *Jobs*: a queue with submit, status, cancel and result. The first
    implementation is in-process. Analysis, proxies and renders all go
    through it.
  - *Identity*: a `Principal` passed to every request. Local mode is a
    single user; an optional bearer token turns on checking.
- **Milestones, in order:**
  1. M1 Hosted-ready platform foundation (SPL-E9): the five seams above.
  2. M2 Agent end-to-end (SPL-E10): import, proxies and thumbnails,
     streaming analysis for multi-hour files, scene and screen signals,
     frame extraction and contact sheets, highlight suggestions,
     apply-an-edit-plan, crop and vertical reframing, export presets,
     captions, render queue, agent guide, an end-to-end agent test.
  3. M3 Frontend rebuild and editor (SPL-E11 to E14): new frontend, a
     trustworthy preview, core human editing (undo, split, ripple,
     snapping, markers, multi-select, speed, fades, keyframes), then
     captions, transcript and audio polish.
  4. M4 Simple by design (SPL-E15): feature budget, first-run flow,
     templates, empty states.
- **Preview: server-side rendering first.** Preview is rendered by the
  engine (proxies and short ffmpeg renders through the job queue), so it
  matches export by construction. A browser compositor (WebCodecs/canvas)
  is allowed only after a time-boxed spike that ends in an explicit go/no-go
  against these criteria: frame-accurate match with export output, smooth
  playback on multi-track edits, and acceptable cost to keep in sync with
  the ffmpeg filter graph. No go, no compositor.
- **Frontend: Svelte 5, Vite and TypeScript**, built to static assets and
  served by FastAPI. The client is generated from `openapi.json`, so the UI
  is just another API consumer. Browser end-to-end tests run in CI. The
  existing vanilla JS pages are ported, then removed.

## Amendments to 0001

- **Auth.** "Auth: none" and "hosting out of scope" are revised to *local
  single user by default, with an identity seam*. The server still binds to
  `127.0.0.1` by default and the file browser stays confined to its root.
- **Persistence.** The sidecar rule still holds for local mode:
  `<video>.split-video-project.json` and `.split-video-cache/` live next to
  the video, never under `~/.cache` or another central path. Hosted storage
  replaces this only behind `ProjectStore` and `MediaStore`; it never
  works by writing to a central path locally.
- 0001 is marked partially superseded by this ADR. Its other decisions
  (browser UI with a local server, Docker supported, one engine) stand.

## Out of scope

Operating a hosted service, accounts and billing, multi-user collaboration,
cloud storage backends, a desktop shell, and any code. This ADR only sets
direction; each milestone is delivered through its own cards.

## Consequences

- M1 changes the on-disk project format (asset ids, revisions). It needs a
  schema version bump and a migration so existing projects keep loading.
- The committed `openapi.json` makes API changes visible in review and
  gives CI a breaking-change check.
- Server-side preview costs render latency. Proxies and caching must keep
  it tolerable, and a compositor spike may later revisit this.
- The frontend rewrite adds a Node toolchain to the repo and the Docker
  build, and the old pages stay in place until their ports land.
- Agents get a stable, documented surface, so API and error-shape changes
  need the same care as UI changes.
