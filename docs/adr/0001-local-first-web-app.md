# 0001: Local-first web app, Docker stays supported

Status: accepted (2026-10-14, recorded 2026-10-19). Board card: SPL-12.
Partially superseded by [0002](0002-agent-first-hosted-ready.md): auth and hosting positions revised.
Amended by [0003](0003-one-editor-one-project-model.md): the split editor and the compose page become one editor with one project model.

## Context

The audience for the next ~6 months is the owner and technical friends.
Recordings are multi-GB, so uploading them to a hosted service is a poor
fit for now. The editor already is a browser app (FastAPI plus vanilla JS)
backed by a local ffmpeg process, and the MCP server and project CLI
(`project.py`, `cli.py`, `mcp_server.py`) already share one headless engine.

## Decision

- **The browser is the UI; a local server does all media work.** The
  browser plays video, draws the timeline and edits project state. The
  server (ffmpeg, YAMNet, waveform, export) runs on the user's machine and
  is reached over localhost. Nothing leaves the machine.
- **Docker remains a supported distribution** (`make dev`, the GHCR image)
  alongside `uv`. A desktop shell (Tauri/Electron) is out for now.
- **One engine, three front doors.** Every editing rule lives in
  `project.py`; the web UI, the CLI and the MCP server are thin adapters.
  New features land in the engine first.
- **State lives in sidecar files next to the video**, not in a central
  path: `<video>.split-video-project.json` (source of truth) and
  `.split-video-cache/` (regenerable). `make dev` mounts only the video
  directory, so anything elsewhere is lost.
- **Auth:** none. The server binds to `127.0.0.1` by default and the file
  browser is confined to the root directory it was started with. Binding to
  another host (needed inside Docker) is an explicit flag. Revisit if a
  hosted service is ever built.

## Out of scope

Building or operating a hosted service, accounts, and cloud storage.

## Must-have editor features (shortlist)

Ordered by how much of the CapCut/Adobe-style workflow they unlock. These
are what SPL-10 and SPL-11 build toward.

1. Multiple source clips in one project, arranged in order.
2. Trim, reorder and delete clips; cut and crossfade transitions.
3. Per-clip audio gain and mute.
4. Multi-track timeline (video and audio tracks, overlays/PiP).
5. Color adjustment (brightness, contrast, saturation).
6. Text/title overlays.
7. Export presets and progress.

## Consequences

- Project-file format needs a versioned schema before clips and tracks
  arrive, so existing single-video projects keep loading.
- Preview of multi-clip and multi-track edits must work in the browser
  without rendering the whole project first.
