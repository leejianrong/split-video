# UX spec: the unified editor

Status: draft for M3 (SPL-91). Decision: [ADR 0003](adr/0003-one-editor-one-project-model.md) (SPL-115).
Layout figures come from the accepted wireframes (1440 x 900 canvas); anything
the wireframes do not settle was decided by the owner on 2026-10-24 and is
recorded under [Decisions](#decisions).

UI cards link to a section with "Implements docs/ux-spec.md#timeline". Anchors
below are stable: do not rename a heading without updating its links.

## Section index

| Anchor | Section |
|---|---|
| [#purpose](#purpose) | Purpose and audience |
| [#principles](#principles) | Principles |
| [#layout](#layout) | Layout regions |
| [#left-tabs](#left-tabs) | Left tabs |
| [#preview](#preview) | Preview |
| [#inspector](#inspector) | Inspector |
| [#timeline](#timeline) | Timeline |
| [#first-run](#first-run) | First run and goal chooser |
| [#export](#export) | Export dialog and render queue |
| [#empty-states](#empty-states) | Empty states |
| [#shortcuts](#shortcuts) | Keyboard shortcuts |
| [#accessibility](#accessibility) | Accessibility |
| [#feature-budget](#feature-budget) | Feature budget |
| [#decisions](#decisions) | Decisions |

## Purpose

This is the design contract for the M3 editor: one workspace, one project (the
timeline project, see [ADR 0003](adr/0003-one-editor-one-project-model.md)). It says what each region shows, what it does
and how it behaves, so frontend cards can be built and reviewed without
re-deciding the design. It does not specify colours, fonts or component code.

Audience: newcomers who have used a CapCut-style editor, working with long
recordings (streams, gigs, lectures). Agents are also users: whatever the UI can
do is available through the API, CLI and MCP (ADR 0002), and what an agent does
is visible in the UI.

Visual direction: dark theme only (no light theme), teal accent. The layout pattern (media and tools
left, preview centre, contextual inspector right, timeline below, export dialog
with a queue) is the one CapCut desktop uses; labels, icons and styling are our
own, and no third-party imagery is used.

## Principles

1. **Familiar layout.** Four regions in the usual places. No mode switches
   between "split" and "compose".
2. **One place for everything.** Every action has a home: a left tab, the
   inspector, the timeline toolbar or the export dialog. No second page.
3. **Sensible defaults.** Snapping is on. Captions off until asked. Export
   presets name the destination ("YouTube 1080p", "Vertical 1080x1920").
4. **Undo always.** Every edit, including agent edits and analysis results that
   change the timeline, can be undone. Undo never needs a confirmation dialog.
5. **No jargon.** Say "cut", "song", "gap", "highlight", not "silence
   threshold", "ripple", "proxy", "job". Technical names may appear in
   tooltips and the advanced section of a panel.
6. **Progress and errors are visible.** Anything slow shows what it is doing and
   roughly how long is left. Every error says what went wrong and one thing to
   try ("Fix: move it back to the recordings folder, then retry").
7. **Agent edits are visible.** An edit from an agent appears live, is marked as
   an agent edit in the history, and is undoable like any other.
8. **Nothing leaves the machine** is stated on first run and in export.
9. **Keyboard and mouse both work.** See [Accessibility](#accessibility).

## Layout

Wireframe sizes at 1440 x 900. Supported window width 1280 to 1920 px; below
1280 px a "window too small" notice is shown rather than a broken layout. Height
minimum 720 px. Touch devices are not supported in M3 (mouse and keyboard only).

| Region | Wireframe size | Min | Resizable | Notes |
|---|---|---|---|---|
| Top bar | 48 px high | 48 | no | Logo, project name and length, save state, aspect ratio picker, agent status, Export button (teal) |
| Left: tab rail | 72 px wide | 72 | no | 7 tabs, icon over label, selected tab has a teal edge |
| Left: panel | 308 px wide | 260 | yes, drag the edge (260 to 480) | Content of the selected tab; collapsible |
| Preview | fills (about 720 px at 1440) | 480 | via the neighbours | Never smaller than 480 px wide |
| Right: inspector | 340 px wide | 280 | yes (280 to 440) | Collapsible |
| Timeline | 292 px high | 200 | yes, drag the top edge (200 to 60% of the window) | Always visible |

Behaviour:

- Total of left (380) + inspector (340) leaves about 720 px for the preview at
  1440 px. At 1280 px the preview is 560 px.
- Narrowing order: first the left panel shrinks to its minimum, then the
  inspector, then the panel and inspector collapse to their edges (the tab rail
  stays). The preview never drops below its minimum.
- Collapsed regions reopen on click of their edge handle or tab.
- Region sizes and the selected tab are remembered per browser (not in the
  project file).
- The top bar shows the save state ("Saved just now", "Saving...", "Not saved:
  retry") and the agent status ("Agent connected, 2 edits live" when an agent
  session is attached; hidden otherwise).
- The aspect ratio picker in the top bar sets the project canvas (16:9, 9:16,
  1:1), which is the output ratio. Changing it never moves clips; it changes crop
  and fit (see [Preview](#preview)).

## Left tabs

The tab rail order is fixed: Media, Highlights, Transcript, Text, Captions,
Audio, Effects. A tab that is a placeholder in M3 is still shown, with a short
"Coming later" panel and no controls, so the layout does not change when it
lands.

| Tab | M3 | Contents |
|---|---|---|
| Media | in | Import, media bin, quick actions |
| Highlights | in | Agent and analysis suggestions to review |
| Transcript | in | Text of the speech, tied to the timeline |
| Text | in (basic) | Title text clips |
| Captions | in | Auto captions from the transcript, style |
| Audio | in (basic) | Music and sound clips, per-clip volume lives in the inspector |
| Effects | placeholder | "Coming later" |

### Media

- **Import** button (primary) at the top; drag and drop onto the panel also
  imports. Imported files appear as cards with a thumbnail, name and length
  (for example "gig-full.mp4, 2:41:08"). Click selects the card, double click or
  drag onto the timeline adds it.
- **Quick actions** section, acting on the selected timeline clip (or the main
  clip if nothing is selected):
  - *Split by silence*: inserts labelled cuts on the main track.
  - *Find songs*: inserts cuts labelled "Song 1", "Song 2" and so on.
  - *Find highlights*: runs analysis; results land in the Highlights tab.
  - *Transcribe*: runs in the background; results land in the Transcript tab.
- Each quick action shows progress inline and can be cancelled. Results that
  change the timeline are one undo step.
- States: empty (see [Empty states](#empty-states)), importing (progress on the
  card), preparing preview (percent on the card; the card is usable meanwhile),
  failed (red badge, message with a fix hint, Retry), missing source (badge
  "File not found", Locate action).

### Highlights

- Header "Suggested highlights" with **Re-run**, and filter chips by recording
  type: Stream, Gig, Lecture, Keyword (the keyword chip takes a word).
- Each suggestion is a card: score (0 to 100), title, time range, and a one-line
  reason ("loud, crowd cheers, you say 'finally'"). Actions: **Make short**
  (builds a 9:16 short with captions on a new timeline segment) and **Add to
  timeline** (inserts the range as a labelled segment).
- Footer button "Make a short from every highlight (N)": creates a short
  timeline for each of the N suggestions listed, and so accepts them. It is
  not the export count (see [Export](#export)).
- A suggestion is **accepted** once the user acts on it with Make short, Add to
  timeline or the footer button. Unreviewed suggestions are never exported.
- Clicking a card previews the range in the preview without changing the
  timeline. Dismissing a card removes it from the list (undoable).
- Review step: suggestions from an agent arrive here and never change the
  timeline by themselves. Agent edit plans that the user has not accepted are
  shown as suggestions.
- States: empty ("No highlights yet. Run Find highlights."), running
  (progress), results, failed (message and fix hint).

### Transcript

- Scrollable text with timestamps, in sentences; the sentence under the playhead
  is highlighted and follows playback. Click a sentence to move the playhead.
- Edit words in place; edits update captions. Deleting text removes nothing from
  the timeline unless the user chooses "Cut selection from timeline".
- Search box finds words and jumps between matches.
- States: not transcribed (button "Transcribe"), running (progress, partial text
  appears), done, failed.
- Speaker labels are not shown in M3 (decided: no diarisation; the transcript is
  plain sentences).

### Text

- "Add title" creates a text clip at the playhead on a Text lane. Style
  (font size, colour, position) is edited in the inspector. M3 has a small
  fixed set of styles; no animation.

### Captions

- "Auto captions from the transcript" (needs a transcript; offers to transcribe
  first). Shows the word count ("118 words").
- Style presets (a row of samples), highlight mode (Highlight, Word by word,
  Line, Off), size, position (Top, Middle, Lower third).
- "Edit words in Transcript tab" link. Captions live on a Captions lane.
- The same settings are used when captions are burned in at export.

### Audio

- Lists audio files from the media bin and "Add to timeline" puts them on a
  Music lane. Per-clip volume and mute are in the inspector. Recording a
  voice-over is not in M3.

### Effects

- Placeholder in M3. Brightness and other adjustments are in the inspector
  Video tab instead.

## Preview

- Shows the timeline at the playhead in the project aspect ratio, letterboxed
  inside the preview region. A time readout "00:41:12 / 2:41:08" sits under it
  with Play/Pause and a **Fit** menu (Fit, 100%).
- Rendering follows ADR 0002: server-side preview, so what is shown matches
  export. While a preview frame renders, the previous frame stays with a small
  spinner; scrubbing may show lower-quality frames, never wrong ones. Targets: a still
  frame within 500 ms of a scrub, and playback at 720p proxy quality. SPL-73
  (browser compositor spike) and SPL-69 to SPL-72 confirm these are feasible;
  if they are not, the targets are revisited.
- **Ratio**: the project canvas (16:9, 9:16, 1:1) set in the top bar; it is the
  output ratio. A clip whose shape differs is shown per its canvas fit: Crop to
  fill, Letterbox, or Reframe (see [Inspector](#inspector)). A clip's reframe
  and crop only choose how that clip is cropped to fit the canvas; they never
  change the canvas, and the canvas never overrides them.
- **Safe zone**: when the ratio is 9:16 a "Safe zone" toggle (default on) draws
  the area clear of platform interface; the Highlights wireframe shows it as a
  checked option. One set of margins for 9:16, no per-platform variants yet: 10%
  of the height free at the top and at the bottom (192 px of 1920) and 6% of the
  width free at each side (65 px of 1080). These are the margins captions
  already keep (`SAFE_MARGIN` and `SIDE_MARGIN` in `src/split_video/captions.py`).
- **Captions and text** are drawn over the picture in the preview as they
  will be exported.
- **Play**: Space or K toggles; clicking the picture toggles. Preview audio is
  the mixed timeline audio.
- While a preview is being prepared (proxy), the preview shows "Preparing
  preview 72%" and editing stays enabled.

## Inspector

The inspector shows tabs for the current selection. Header line states the
selection, for example "Selected: Song 2, 4 min 12 s".

| Selection | Tabs and controls |
|---|---|
| Nothing | Project details: name, source file(s), length, aspect ratio, frame rate, "proxy/preview: ready". Export shortcut. |
| Video clip (including a song segment) | **Video**: Canvas fit (Crop to fill, Letterbox, Reframe), Scale (%), Position (X, Y), Brightness; Reset clip. **Audio**: Volume (dB), mute. **Speed**: speed factor. **Crop**: crop region. |
| Audio clip | Volume (dB), mute, fade in and out. |
| Text clip | Text, size, colour, position. |
| Captions clip | Style, highlight mode, size, position, "Edit words in Transcript tab". |
| Cut / boundary between segments | Label, time (editable), "Remove cut" (merges the segments). |
| Several clips | Only controls valid for all of them: volume, speed, scale, position; others hidden. |
| Highlight card (Highlights tab) | Same as Nothing, plus the card's range and actions. |

Rules:

- Changing a control applies immediately and is one undo step per drag or edit.
- Every numeric field has a reset. "Reset clip" restores clip defaults.
- The output ratio is the project canvas in the top bar; the inspector has no
  per-clip ratio. Canvas fit, Reframe and Crop only decide how the clip is
  cropped to fit the canvas.
- Reframe: "Follow centre" or "Pick region" (drag a box on the preview). Smart
  tracking is not in M3.
- Keyframe buttons are not shown in M3.

## Timeline

Bottom region, 292 px high in the wireframe.

**Toolbar** (40 px): Select tool, Undo, Redo, Split at playhead, Delete, a
Snapping toggle (on by default; the label "Snapping on" appears in the status),
a single global Ripple toggle (off by default), zoom out / slider / zoom in, and a "fit whole timeline" button.

**Ruler and playhead.** A time ruler across the top (labels adapt to the zoom:
seconds up to hours; the wireframes use 0:30:00 steps for a 2 h 41 min
recording). A vertical playhead; click or drag the ruler to move it. The
playhead position is shown in the preview readout.

**Tracks and lanes.** Each lane has a header (120 px wide) at the left. Lanes,
top to bottom, appear only when they have content except the main lanes. A Zoom
or effects lane is post-M3: zoom clips have no creation path in M3 and the lane
is not shown.

| Lane | Contents |
|---|---|
| Text | Title clips |
| Captions | Caption blocks, labelled "Auto captions, 1,204 words" |
| Main video | The main track: segments with labels, filmstrip |
| Main audio | The main track's audio: waveform |
| Music | Added audio clips |

Lane headers carry **lock**, **hide** (video lanes) and **mute** (audio lanes)
toggles and the lane name ("Main · Video"). A locked lane cannot be edited and
shows a lock mark; a hidden lane is not drawn in preview or export.

**Segments and labels.** Cuts on the main track split it into labelled
segments ("Opening", "Song 1", "Chat"). The label sits on the segment, truncated
with an ellipsis, with the full text in a tooltip. Double click edits the
label. Segments of the same type may share a colour; Song, Chat and Opening are
the label vocabulary the analysis produces, but any text is allowed.

**Filmstrip and waveform.** Video segments show a filmstrip of thumbnails, more
of them as zoom increases. The audio lane shows the waveform. Both come from
cached derived data and fill in progressively; a placeholder is drawn until they
are ready.

**Zoom.** Slider and buttons, Ctrl + mouse wheel, `+` and `-`. Zoom keeps the
playhead (or the pointer when using the wheel) at the same screen position. The
default view fits the whole timeline.

**Snapping.** On by default. Edges, the playhead, cuts and markers attract
within 8 px; a thin teal guide line shows what was snapped to. Holding Alt while
dragging turns snapping off for that drag.

**Selection.** Clicking a clip selects it (and moves nothing); Shift-click adds;
drag on empty space draws a box. A plain click on a lane (empty space or
between clips) moves the playhead to that point and deselects. The playhead also
moves on the ruler.

**Drag and trim.** Drag a clip to move it (within its lane, or to a compatible
lane). Drag a clip edge to trim; a tooltip shows the new time. Drag a cut to
move the boundary between two segments; the neighbours resize with it and
nothing is lost. Minimum clip length is one frame.

**Ripple.** One global toggle for the whole timeline, off by default. With
ripple on, delete or trim closes the gap on that lane and moves later clips on
it. With it off, delete leaves a gap, on the main track too. Shift+Delete
ripple-deletes the selection whatever the toggle says.

**Split at playhead.** `S` (Ctrl/Cmd+B as an alias) or the toolbar button cuts the clip(s) under the
playhead on every unlocked lane (selected clips only if any are selected). The
result is two segments with the same label plus " (2)" until renamed.

**Undo and redo.** Unlimited within a session, shared by UI and agent edits.
History is session-only: it is not saved in the project file and is gone after a
reload (persisting it would be a format change, later).

**Agent edits.** Appear live; changed segments flash briefly and an entry
"Agent: removed 3 gaps" appears in the undo list.

**Long timelines.** Hours-long timelines stay interactive: only what is visible
is drawn; zooming never blocks on analysis.

## First run

Shown when the editor opens with no project (and from "New project").

1. **Add your recording.** A drop zone ("Drop a video here, or choose a file
   from your recordings folder. Streams, gigs and lectures of any length
   work.") and a **Choose file** button. The text "Everything stays on this
   computer" is shown on the page. After a file is chosen its name and length are
   shown ("gig-full.mp4, 2 h 41 min") with "Preparing preview 72%".
2. **What do you want to make?** Four goal cards:

   | Goal | What happens |
   |---|---|
   | Split into songs | Finds the gaps and cuts one segment per song; labels them |
   | Find highlights | Suggests moments, opens the Highlights tab to review |
   | Make vertical shorts | Highlights cropped to 9:16 with captions |
   | Start with an empty timeline | The video as one clip; nothing runs |

3. **Progress** is shown on the same page while preparing: "Listening for
   silences and music", "Building a fast preview, about 1 min left",
   "Transcribing speech (optional, runs in the background)". Each line has a
   state (waiting, working, done, failed).
4. **Open the editor** (primary) is enabled as soon as the preview is usable;
   **Skip, I'll choose later** opens the editor with the empty-timeline result.

Rules: the goal can be changed later from the Media tab; nothing the goal
started blocks the editor; a failed step shows a fix hint and Retry on this
page and in the editor.

## Export

A dialog opened from the top bar Export button ("Export and render queue").

**What to export** (radio):

| Option | Output |
|---|---|
| Each segment as its own file | One file per labelled main-track segment, named from the labels ("Song 1 ... Song 4, 4 files") |
| The whole timeline as one video | One file, named after the project ("my-gig.mp4") |
| Each highlight as a short | One vertical clip with captions per accepted highlight (see [Highlights](#highlights)); unreviewed suggestions are not exported |

**Settings:**

- Format preset: YouTube 1080p, Vertical 1080x1920, Square 1080, Draft 720p.
- "If the shape differs": Crop to fill or Letterbox.
- Captions: Burned in or Off, with the style ("Style: Shorts").
- Save to: a folder path with a Choose button; default is a folder next to the
  source named `<source>_export`.
- A line "Estimated time: about 4 min, up to 2 renders at once".
- Footer: summary of presets, Cancel, and **Start export (N)** (N = files to render: segments, one, or accepted highlights). N is not the count on the Highlights footer button.

**Render queue** (below the settings, also reachable from the top bar after the
dialog closes):

- One row per output file with name and state: Waiting, Rendering with a percent
  and bar, Done (Open folder), Failed (message, "Fix: ..." hint, Retry; for example "my-gig.mp4: out of disk space. Fix: free some space, then retry"),
  Cancelled.
- **Cancel all**, and per-row cancel. One failed render never stops the others.
- The dialog can be closed while renders continue; the top bar shows a queue
  badge with the count of active renders.
- Queue state comes from the job queue, so it survives closing the window.

## Empty states

| Where | Message and action |
|---|---|
| No project | First-run page |
| Media tab, no media | "Add your first recording" with the Import button and a drop hint |
| Highlights tab | "No highlights yet. Run Find highlights." |
| Transcript tab | "Not transcribed yet." with Transcribe |
| Captions tab, no transcript | "Captions are made from the transcript." with Transcribe |
| Timeline, empty | "Drag material here and start to create" plus the Import button |
| Inspector, nothing selected | Project details |
| Render queue, empty | "Nothing rendering. Choose what to export and press Start." |
| Placeholder tabs | "Coming later." |

## Shortcuts

Shortcuts are inactive while a text field has focus, except Escape and the
Ctrl/Cmd combinations. Current = already in the editor today
(`src/split_video/editor/static/shortcuts.js`, `timeline.js`); new = to build.
The `?` overlay lists exactly this table.

| Keys | Action | Status |
|---|---|---|
| Space / K | Play / pause | current |
| J / L | Step back / forward 5 s | current |
| S | Split at the playhead (adds a cut) | current (was "add a split") |
| Ctrl/Cmd+B | Split at the playhead | new, CapCut-like alias |
| Delete / Backspace | Delete the selected clip or cut (a cut: remove the cut) | current, widened |
| Shift+Delete | Ripple delete: delete the selection and close the gap | new |
| Left / Right | Nudge the selected cut 0.1 s (1 s with Shift) | current |
| Comma / Period | Previous / next frame | new |
| Home / End | Playhead to start / end | new |
| Up / Down | Playhead to previous / next cut | new |
| Ctrl+Z | Undo | new |
| Ctrl+Shift+Z / Ctrl+Y | Redo | new |
| Ctrl+A | Select all clips on unlocked lanes | new |
| Ctrl+D | Duplicate the selection | new |
| Ctrl+S | Save now | new |
| Ctrl+E | Open Export | new |
| Ctrl + wheel, `+` / `-` | Zoom the timeline | new |
| Shift+Z | Fit the whole timeline | new |
| N | Toggle snapping | new |
| Alt (held while dragging) | Snapping off for that drag | new |
| Escape | Close dialog, clear selection | new |
| ? | Toggle the shortcut list | current |

On macOS Ctrl reads as Cmd.

## Accessibility

- **Focus order:** top bar, tab rail, left panel, preview controls, inspector,
  timeline toolbar, timeline lanes. Tab moves between regions; arrow keys move
  inside a tab rail, lane or list. A visible focus ring (teal, 2 px) on every
  control.
- **Labels:** every icon button has an accessible name and a tooltip with its
  shortcut. Regions are landmarks (banner, navigation for the rail, main,
  complementary for the inspector). Progress bars expose their value.
- **Contrast:** text and controls meet WCAG AA (4.5:1 text, 3:1 controls and
  focus rings) on the dark theme. Teal is never the only signal: states also
  have an icon or text (locked, hidden, failed).
- **No mouse-only actions:** every timeline action (select, move, trim, split,
  delete, label, zoom) has a keyboard path: select a clip with arrow keys, move
  with Alt+arrows, trim with Shift+Alt+arrows, and the rest via the table above
  or the inspector's numeric fields.
- Errors and completion are announced through a polite live region. Motion
  (flashes for agent edits) respects reduced-motion settings.
- Text is at least 12 px and scales with browser zoom up to 200 percent without
  losing controls.

## Feature budget

| Feature | M3 | Deferred | Out of scope |
|---|---|---|---|
| Import, media bin | x | | |
| Split by silence, find songs, labelled cuts | x | | |
| Highlights review and make short | x | | |
| Trim, split, move, ripple, snapping, undo/redo | x | | |
| Multi-select, markers | x | | |
| Speed, fades, per-clip volume | x | | |
| Transcript and auto captions | x | | |
| Basic text titles | x | | |
| Vertical reframe (centre or region) | x | | |
| Export presets, render queue | x | | |
| Aspect ratio 16:9, 9:16, 1:1 | x | | |
| Effects tab contents, transitions | | x | |
| Keyframes | | x | |
| Loudness normalisation | | x | |
| Voice-over recording | | x | |
| Smart reframe tracking | | x | |
| Stickers | | | x |
| Stock media library | | | x |
| AI generation (media, avatars, voices) | | | x |
| Accounts, cloud projects, collaboration | | | x |
| Desktop shell | | | x |

Deferred items keep their place in the layout where it costs nothing (the
Effects tab) and get no controls.

## Decisions

Owner decisions of 2026-10-24 on the gaps and inconsistencies found in the
wireframes. The sections above already reflect them.

1. **Playhead on click.** A plain click on a lane moves the playhead; clicking a
   clip selects it.
2. **Ripple.** A single global toggle, off by default. Shift+Delete
   ripple-deletes the selection.
3. **Safe zone.** One set of margins for 9:16 (10% top and bottom, 6% at each
   side, as captions use), shown by a toggle. No per-platform variants yet.
4. **Undo and redo** are session-only. History is not persisted; persisting it
   would be a format change, later.
5. **Aspect ratio in two places.** The project canvas ratio (top bar) is the
   output ratio. A clip's reframe and crop in the inspector control how that
   clip is cropped to fit the canvas. They do not override each other.
6. **Each highlight as a short** exports only highlights the user has accepted.
   The Highlights footer button "Make a short from every highlight" creates
   short timelines for the listed suggestions; the export count (Start export
   (N)) is the number of files to render.
7. **Export names.** The whole-timeline file is named after the project. The
   failed-render example uses a name an export can produce.
8. **Preview targets.** A scrubbed still within 500 ms; playback at 720p proxy
   quality. SPL-73 and SPL-69 to SPL-72 confirm feasibility, otherwise revisit.
9. **Window.** Narrower than 1280 px shows a "window too small" notice;
   minimum height 720 px; no touch support in M3.
10. **Dark theme only.**
11. **Shortcuts.** S splits at the playhead (Ctrl/Cmd+B alias); Delete removes the
    selected clip or cut; Shift+Delete ripple-deletes.
12. **Transcript speakers.** No speaker labels in M3.
13. **Lane list.** Lanes appear only when they have content, except the main
    video and main audio lanes.
14. **Zoom lane.** Post-M3. There is no Zoom lane in M3 (zoom clips have no
    creation path); the Effects tab stays a placeholder.
