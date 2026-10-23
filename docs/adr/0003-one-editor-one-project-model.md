# 0003: One editor, one project model

Status: accepted (2026-10-23). Board card: SPL-115. Amends 0001 and 0002.
UX contract: `docs/ux-spec.md` (SPL-91, separate PR).

## Context

The product grew two editors:

- **Split editor** (SPL-1 to SPL-14): open one video, split it by silence,
  adjust split points, export segments. State is
  `<video>.split-video-project.json`.
- **Compose** (SPL-15 to SPL-29): a multi-clip timeline with tracks, overlays
  and render. It has its own page and its own state,
  `<name>.split-video-timeline.json`.

Two pages, two files and two sets of rules for the same recording. A user who
splits a gig into songs and then wants a highlight reel has to move between
them by hand. ADR 0002 planned the M3 frontend rebuild as two ports (one per
page), which would carry the split into the new code. The audience is also
widening from the owner and technical friends to newcomers who know CapCut-style
editors, and those users expect one workspace.

## Decision

- **One editor, one project model.** The timeline project
  (`*.split-video-timeline.json`) is the model. Opening a video creates a
  timeline with that video as one clip on the main track. There is no separate
  split editor.
- **Splitting is a timeline operation.** "Split by silence" and "Find songs"
  insert labelled cuts on the main track. Splitting into files is an export
  mode: "each segment as its own file", named from the labels.
- **The engine holds the rules.** Cut, label, trim and ripple logic lives in
  the engine. The editor, the CLI, MCP tools and the HTTP API stay thin
  adapters over it. The CLI, MCP and API split commands keep their names,
  arguments and output shape; they now read and write the timeline project.
- **Layout follows CapCut desktop.** A tab rail and panel on the left (Media,
  Highlights, Transcript, Text, Captions, Audio, Effects), preview in the
  centre, a contextual inspector on the right, the timeline along the bottom,
  and an export dialog with a render queue. Layout, labels and styling are our
  own. Dark theme with a teal accent. Details are in the UX spec.
- **Audience for now: CapCut-level newcomers.** Defaults favour them (snapping
  on, undo always, no jargon, errors with a fix hint).
- **Agent edits show live.** Changes made through the API, CLI or MCP appear in
  the open editor over SSE (SPL-68) without a reload.
- **Highlights tab is a review step.** Agent suggestions land there. The user
  accepts them onto the timeline or turns them into shorts.
- **First run asks for a goal.** After a video is added the user picks one of:
  split into songs, find highlights, make vertical shorts, or an empty timeline.
  Skipping is allowed.
- **Order of work.** Engine unification (SPL-116) happens first, on the
  current frontend. The Svelte rebuild (M3) then builds one editor on the
  unified engine.
- **Deferred out of M3:** keyframes and loudness normalisation. 0002 listed
  keyframes in M3; that list is amended here.

### Migration principles

- An existing `*.split-video-project.json` opens as a timeline with no data
  loss: source, split points, labels and export settings all carry over.
- The original file is left untouched until the user saves; the first save
  writes the timeline project next to it. It is never deleted automatically.
- Public formats (`*.split-video-project.json`, `*.split-video-timeline.json`,
  `.split-video-assets.json`) change only with a version bump and a tested
  migration.
- Migration is the engine's job (behind `ProjectStore`), so every adapter gets
  it and nothing is converted in the browser.
- Sidecar files stay next to the video, as in 0001.

## Amendments

- **To 0001:** "Every editing rule lives in `project.py`" now reads as the
  timeline engine. The must-have shortlist is delivered inside the one editor,
  not a second page. The split project file is a legacy input, not the
  source of truth.
- **To 0002:** the M3 frontend is one port, not two. The "core human editing"
  list drops keyframes. Loudness normalisation, if it was planned under audio
  polish, moves out of M3. The other 0002 decisions stand.

## Consequences

- One page and one file format to maintain, and no hand-off between tools.
- Split behaviour becomes a rendering/export concern; the existing CLI, MCP and
  API split commands need adapters that map to timeline cuts, and tests that
  pin their output shape.
- Old projects need a migration and tests with real legacy files.
- The M3 cards must be re-scoped against the UX spec.
- A newcomer-first layout leaves less room for expert shortcuts; keyboard
  support still has to be complete.

## Alternatives considered

- **Keep two editors and link them.** Cheapest, but keeps two models and the
  manual hand-off.
- **Make the split project the model and add tracks to it.** It has no clip or
  track structure; the timeline format already does.
- **Wait for the Svelte rebuild, then unify.** Ports two UIs first, then merges
  them. Unifying the engine first removes that double work.
- **Copy a desktop editor's feature set.** Too large; the spec carries a
  feature budget instead.

## Out of scope

Code, the Svelte rebuild itself, keyframes, loudness normalisation, stickers,
stock media, AI generation, accounts and hosting (see 0002), and the visual
design details, which belong to the UX spec.
