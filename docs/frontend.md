# The new editor frontend (scaffold)

The editor is being rebuilt as one Svelte 5 app (decisions: [ADR 0004](adr/0004-frontend-stack-and-structure.md),
layout contract: [ux-spec.md](ux-spec.md)). Today it has the shell (top bar, tab rail and panel, preview, inspector, timeline), a start page and a library, the
Media tab (see [below](#the-media-tab)), the timeline (see [below](#the-timeline)) and empty panels for the other tabs, served at **`/app/`**. The old editor at `/` and `/compose.html` is unchanged.

## Views and the shell

One hash selects the view ([ADR 0004, Routing](adr/0004-frontend-stack-and-structure.md#routing)); there is no router
library. `lib/state/route.ts` parses and formats it, and is the only code that reads `location`:

| Hash | View |
|---|---|
| `#/` | Start: "Add your recording" (opens the library, or the video the server was started on) and the goal list |
| `#/library`, `#/library/<folder>` | Library: folders and videos from `GET /api/v1/browse`; loading, error and empty states |
| `#/p/<project id>` | The editor. The id is the video's path; `POST /api/v1/open` loads it (the timeline project, ADR 0003) |

A reload on `#/p/<id>` opens the same project again; an unknown id shows an error page with a way back to the library.
Leaving the editor closes the project in `lib/state/project.svelte.ts`, so the next open reads it fresh (it holds the
project and the server revision it was read at; load only until the edit cards land).

The editor shell is `lib/shell/EditorShell.svelte`, a CSS grid of the ux-spec regions (1440 x 900 sizes in brackets):

| Region | Element | Size |
|---|---|---|
| Top bar | `header` (`data-region="top-bar"`) | 48 px high, fixed |
| Tab rail | `nav` inside `Tabs variant="rail"` | 72 px wide, fixed; Media, Highlights, Transcript, Text, Captions, Audio, Effects |
| Left panel | the tab's `tabpanel` | 260 to 480 px (308) |
| Preview | `main` | the rest, never under 480 px (720) |
| Inspector | `aside` | 280 to 440 px (340) |
| Timeline | `section` | 200 px to 60% of the window height (292), full width at the bottom |

Below 1280 px wide the editor shows a "window too small" notice instead of the grid. The regions hold no logic; panel
contents, preview, inspector controls and timeline lanes come in later cards.

**Resizing.** A `ResizeHandle` sits on the panel's right edge, the inspector's left edge and the timeline's top edge
(an ARIA window splitter: `role="separator"`, `aria-valuenow/min/max`). Drag it, or focus it and use the arrow keys
(10 px, Shift 50 px), Home and End for the limits; double click or Enter resets that region. Limits live in
`lib/state/layout.ts` (a test keeps them equal to the `--size-*` tokens). When the window narrows, the panel shrinks
first, then the inspector, so the preview keeps 480 px. Sizes and the selected tab persist in `localStorage` under
`split-video.layout.v1` (try/catch; the layout works without it) and never go in the project.

## Docker

The image builds the app in a Node stage and copies only the result in, so there is nothing to install:

```bash
make dev        # then open http://localhost:8765/app/
```

## From a checkout

Node 24 (see `frontend/.nvmrc`) is needed for the frontend only.

```bash
make frontend        # npm ci && npm run build, into src/split_video/editor/static_app/
uv run split-video edit videos/   # /app/ now serves the build
```

Without a build, `/app/` answers 404 with a hint to run it, and the old pages work as before. `static_app/` is
gitignored; it is never committed.

## Dev loop

```bash
uv run split-video edit videos/       # API on 127.0.0.1:8765
make frontend-dev                     # Vite on http://localhost:5173/app/, HMR
```

Vite proxies `/api` and `/media` (including the `/api/v1/events` SSE stream) to port 8765, so the browser sees one
origin and FastAPI needs no CORS settings.

## Scripts (`frontend/`)

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server with the proxy |
| `npm run build` | Production build into `src/split_video/editor/static_app/` |
| `npm run check` | `svelte-check` with warnings as errors (includes a11y warnings) |
| `npm run lint` | ESLint and `prettier --check` |
| `npm run format` | `prettier --write` |
| `npm run test` | Vitest |
| `npm run api:generate` | Regenerate the typed API client from `../openapi.json` (also `make api-client`) |

CI runs the same set in the `frontend` job (plus the API client drift check below), then builds the wheel and sdist and runs
`scripts/check_wheel_contents.py` on them.

## API client

`frontend/src/lib/api/generated/` is generated from the committed `openapi.json` by
[`@hey-api/openapi-ts`](https://heyapi.dev) (exact-pinned, config in `frontend/openapi-ts.config.ts`): types, one SDK
function per operation and the fetch client. It is committed, so a route change shows up as a type diff in the same PR,
and it needs no Python to regenerate. After `make openapi schemas errors` run **`make api-client`** (or
`npm run api:generate` in `frontend/`); CI and the pre-push hook fail on a stale or untracked generated file.
The generated directory is excluded from eslint and prettier but is type-checked by `npm run check`.

Things to know about the output:

- Only `/api/v1` is in the schema (the deprecated unversioned aliases are not), so there is one function per
  operation. Names come from FastAPI's operation ids, so they are long (`whoamiApiV1WhoamiGet`,
  `listAssetsApiV1AssetsGet`); fixing that means setting `operation_id`s in the backend, not renaming in the frontend.
- The client throws on failure (`throwOnError: true`), and success returns `{ data, request, response }`.

`frontend/src/lib/api/client.ts` is the hand-written layer, and the only place components import the API from:

- `ApiError` (`code`, `message`, `fix`, `status`, `details`, `currentRevision`, `isRevisionConflict`): every failed call
  rejects with it, built from the error catalogue body ([errors.md](errors.md)). A body of another shape gives
  `code: "unknown"` and a message from the status; no response at all gives `code: "network"`, `status: 0`.
- `configureClient({ token, baseUrl })`: the optional bearer token (sent as `Authorization`) and base URL. The default
  is same origin, so it works behind FastAPI at `/app/` and behind Vite's proxy in dev.
- `ifMatch(revision)`: spread into a write's options to send `If-Match: "<revision>"` ([revisions.md](revisions.md)).
  A stale write rejects with `error.isRevisionConflict` and `error.currentRevision`.
- `connectEvents({ onEvent, onStatus, query })`: `GET /api/v1/events` with reconnect and exponential backoff (500 ms
  doubling to 15 s, reset when the stream opens). `onStatus` reports `connecting`, `open`, `reconnecting` and `closed`;
  an `open` with `reconnect: true` means refetch what you mirror. An `EventSource` can be injected for tests. Browsers
  cannot send the bearer token on an `EventSource` (see [api.md](api.md)).

The top bar's server status line (`lib/shell/ServerStatus.svelte`) calls `GET /api/v1/whoami` through this client.

## Components, tokens and shortcuts

The look is a set of CSS custom properties in `frontend/src/lib/styles/tokens.css` (colour, spacing, radii, type,
z-index, focus ring, region sizes). Dark theme only ([ux-spec decision 10](ux-spec.md#decisions)), but every colour
is a token, so a second theme is a second set of values. Components use tokens and nothing else: a Vitest test fails
on a hard-coded colour under `src/lib`, and `contrast.test.ts` checks every text and control pair for WCAG AA. Add a
pair there when a component uses a new one.

The base components are in `frontend/src/lib/ui/`: `Button` (primary, secondary, ghost, danger; sm, md, lg),
`IconButton` (needs a `label`, shows it and its shortcut in a `Tooltip`), `Slider`, `Panel`, `Dialog` (native
`<dialog>`: focus trap, Escape, focus returns to the opener), `ToastHost` with the `toasts` store (polite live region;
errors stay until dismissed), `Tooltip`, `Tabs`, `TextField`, `Select`, `Checkbox`, `ShortcutHelp` (the `?` overlay)
and a small `Icon` set.

**Dev page:** open `/app/#/dev` (or `http://localhost:5173/app/#/dev` under `make frontend-dev`). It shows every
component, variant and state, toast and dialog triggers, the shortcut list and a table of the tokens. It is not linked
from the shell and is a separate chunk, so the normal page does not load it. Add a component there in the same PR.

**Shortcuts** are data in `frontend/src/lib/a11y/shortcuts.ts`, and are exactly the table in
[ux-spec.md#shortcuts](ux-spec.md#shortcuts); a test parses that table and fails if either side drifts. To add one:

1. add the row to the ux-spec table;
2. add the entry to `SHORTCUTS` (id, `keys` and `label` as in the table, group, `when`);
3. in the component that owns the behaviour, call `registerAction('your.id', handler)` from an `$effect` (it returns
   the function that unregisters). A second handler for the same id throws.

One `keydown` listener (`a11y/dispatcher.ts`) does the dispatch. It skips text fields except Escape and Ctrl/Cmd
combinations (undo, redo and select all stay with the field), maps Ctrl to Cmd on macOS, and an open `Dialog` switches
the editor shortcuts off. `announce(message)` in `a11y/announce.ts` speaks a message through a polite live region.

## The Media tab

`lib/tabs/media/` implements [ux-spec.md#left-tabs](ux-spec.md#left-tabs) (Media). What is where:

| Piece | File | Notes |
|---|---|---|
| Library | `state/assets.svelte.ts` (`AssetsStore`) | `GET /api/v1/assets` (the workspace's asset table). The open recording is registered into it (`ensure`) so it is always a card. |
| Jobs and the event stream | `state/jobs.svelte.ts` (`JobsStore`) | The one `EventSource` of the page (`connectEvents`: reconnect with backoff, and a refetch of `GET /jobs` after a reconnect). Started by `state/live.ts` from the editor shell; `project` events move the project store. |
| Quick actions | `state/quickActions.svelte.ts` | One run per asset and action; progress is read from the jobs store. |
| Selection | `state/selection.svelte.ts` | The selected library asset. |
| Add to the timeline | `ProjectStore.addAsset` in `state/project.svelte.ts` | `POST /project/clips` with `If-Match: <revision>`; edits are serialised; a 409 reloads the project and sets `project.editError` ("changed somewhere else"). |
| Drag payload | `state/dnd.ts` | See below. |

**Import.** Two ways, because a browser cannot hand the server a path:

- *Import* (the button) and a **drop of files from the computer** on the panel upload each file (`uploadFile` in
  `api/client.ts`, XHR for the progress): `POST /api/v1/assets/upload`, one file at a time. The card shows "Importing 42%"
  with a Cancel (the server removes the partial file), or "Import failed" with the catalogue's message, `Fix:` and Retry.
  The file is **copied into the workspace** (the recordings folder), so it survives `make dev`'s `--rm` container.
- The **folder icon** opens "Add from your recordings folder": files already in the workspace are added to the library
  where they are, with no copy (`POST /assets/register`). Use this for a very large recording.

A video that was imported gets its preview prepared (a `proxy` job); the card shows "Preparing preview 72%" and stays usable.

**Cards.** A thumbnail (`GET /assets/{id}/frame`: one ffmpeg seek, 240 px wide, a frame 10% in and at most 30 s in, lazy
loaded, never a whole decode), the name and the length. States: importing, failed, preparing preview, missing source
("File not found" and **Locate**, which asks the server to find the file again by its content) and audio (an icon, no
picture). Click or Space selects. **Double click or Enter adds it to the timeline** (the keyboard equivalent of the double
click); every card also has an *Add to timeline* button (shown on hover, focus and selection). Arrow keys move between cards.
Audio-only assets cannot go on the main video track yet (the button says why).

**Quick actions** run on the selected card, or on the open recording when none is selected (the spec says "selected
timeline clip": there are no clips to select until the timeline card, so this is the closest honest version).

| Button | Calls | Job |
|---|---|---|
| Split by silence | `POST /assets/{id}/split` (labels "Part 1", "Part 2" ...) | `split` |
| Find songs | `POST /assets/{id}/split` with `label_prefix: "Song"` | `split` |
| Find highlights | `POST /assets/{id}/report/prepare`, then `GET /assets/{id}/highlights` for the count | `analysis` |
| Transcribe | `POST /assets/{id}/transcribe` | `transcription` |

"Find songs" and "Split by silence" are the same engine operation today (a song is what the silence gaps leave: pieces
shorter than 30 s are merged away), differing in the labels. Each shows a percent and Cancel (a real cancel: the job
stops ffmpeg), then "3 songs found", or the error with its `Fix:` and Retry (for Transcribe without the add-on: the
install hint). A split saves with the open project's revision and reloads the project, so the timeline shows the pieces.
Highlights results are only counted for now: the review list is the Highlights tab. Completion and failures are spoken
through the polite live region (`announce`).

The timeline that shows the result is [below](#the-timeline).

### Dragging an asset onto the timeline

The payload is defined in `state/dnd.ts` (with `setAssetDrag`, `hasAssetDrag`, `readAssetDrag`):

- MIME type **`application/x-split-video-asset`**, JSON `{"v":1,"assetId":"a_...","name":"gig.mp4","duration":9668.2}`, plus
  `text/plain` with the file name. `effectAllowed` is `copy`.
- A target accepts in `dragover` after `hasAssetDrag(event.dataTransfer)` (only the types are readable until the drop) by
  calling `preventDefault()`, and in `drop` reads `readAssetDrag(event.dataTransfer)` and adds the asset at the drop position
  (the timeline does it with `dropOps` and one `project.applyOps`; `project.addAsset(assetId, index)` stays for the Add button). The keyboard alternative is the card's Enter key and its *Add to timeline* button.
- Files dragged from the computer carry `Files` instead (`hasFileDrag`); the Media panel imports those, the timeline
  ignores them.

## The timeline

`lib/timeline/` implements [ux-spec.md#timeline](ux-spec.md#timeline): lanes with headers, a ruler, the playhead, zoom,
selection, a filmstrip and waveform on the main track, and drag, trim and keyboard editing. It draws `project.timeline`
(`GET /project/timeline`) and holds no editing rule: every gesture ends in **one** call, `project.applyOps(ops)`
(`POST /project/ops`, [edit ops](edit-ops.md) with `If-Match`), and the engine says yes or no. Undo will wrap that one
function. A stale revision reloads the project and shows "This project changed somewhere else, so your change was not
applied" (the same path as every edit); a refused edit (a locked lane, an overlap) shows the engine's message and fix.

| Piece | File | Notes |
|---|---|---|
| Mapping | `timeline/scale.ts` | Pure: `x = time * pxPerSecond - scrollLeft`, zoom around an anchor, limits (the whole timeline to 400 px/s), the log zoom slider, adaptive ruler ticks (0.05 s to 4 h spacing), `visibleSlice` (two binary searches). |
| Lanes | `timeline/lanes.ts` | Tracks to rows: overlay and text tracks (latest on top), Main · Video, Main · Audio, then audio tracks ("Music"); a lane exists only with content, except the two main ones. |
| Edits | `timeline/edits.ts`, `drag.svelte.ts` | Pointer maths to ops: `trim_clip` (clamped to the source, the shortest clip and, on a free track, the neighbours), `move_clip` (main track: the index among the clips by their middles), `place_clip` (free tracks: time and lane of its kind), `add_clip` for a dropped card. One commit on release; Escape cancels. |
| View | `state/timelineView.svelte.ts` | Zoom and scroll; starts fitted; Ctrl+wheel zooms around the pointer, `+` `-` around the playhead, Shift+Z fits, Shift+wheel scrolls. |
| Playhead | `state/playhead.svelte.ts` | Click or drag the ruler or the red head; the preview readout follows. The preview does not play yet. |
| Selection | `state/clipSelection.svelte.ts` | Click, Shift- or Ctrl-click adds, Escape clears; the inspector header names it ("Selected: Song 2, 4 min 12 s"). |
| Filmstrip | `timeline/thumbs.ts`, `state/thumbs.svelte.ts` | Frames from `GET /assets/{id}/frame` for the visible part of each clip only, on a time grid shared across zoom levels, 4 requests at a time, never the same URL twice; a tile that has not arrived is not drawn. |
| Waveform | `timeline/waveform.ts`, `state/waveforms.svelte.ts` | From `GET /assets/{id}/waveform` (the server decodes once, as a `waveform` job; "Preparing the waveform..." until then). The finer of the two stored curves is drawn as columns for the visible part. |

**Performance.** Only the clips that touch the screen (plus 240 px) are elements, found with binary searches; ticks, tiles
and waveform columns are the visible ones; wheel zoom is coalesced to one update per frame. A 3 h project with 108 segments
has about 500 DOM nodes at any zoom, and a zoom sweep from the fit to the closest level and back keeps every frame under 34 ms.

**Keyboard** (lanes are listboxes of options with a roving tabindex, no `application` role): Left and Right move to the
previous and next clip and select it, Home and End to the first and last; **Alt+Left/Right** moves the clip (main track:
one position; other tracks: 0.1 s, or 1 s when zoomed far out); **Shift+Alt+Left/Right** trims the clip's end by the same
step, and with Ctrl also held its start. The result is spoken ("Moved Song 3 to position 4 of 109", "Trimmed Song 3, now
1 min 28 s long, starts at 0:04:32"). The ruler is a slider (arrows 1 s, Shift 10 s). The lane header's lock, hide (video
lanes other than the main one) and mute (audio lanes) buttons are `aria-pressed` toggles and change the track's flags
through `set_track_flags`.

**Not yet** (later cards): snapping, ripple, undo and redo, split at the playhead, delete, box selection, dragging a cut
between two segments, double click to rename, a Captions lane (the model has no caption clips), and playback.

## Caching

Vite content-hashes everything under `/app/assets/`, so those files are served `public, max-age=31536000,
immutable`. `/app/index.html` (which names the hashes) and the old pages stay `no-store`.

## Packaging

The wheel and sdist must contain `split_video/editor/static_app/`: pip users have no Node. `uv_build` packs
gitignored files under the module root, so running `npm ci && npm run build` in `frontend/` before `uv build` is all
that is needed. Check it with `uv build --out-dir dist && python3 scripts/check_wheel_contents.py dist`. The
repository has no job that publishes the wheel or sdist yet (a `v*.*.*` tag only publishes the Docker image); the
job that does must run the build first, as the `frontend` CI job does.
