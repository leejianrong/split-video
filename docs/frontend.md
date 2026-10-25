# The new editor frontend (scaffold)

The editor is being rebuilt as one Svelte 5 app (decisions: [ADR 0004](adr/0004-frontend-stack-and-structure.md),
layout contract: [ux-spec.md](ux-spec.md)). Today it has the shell (top bar, tab rail and panel, preview, inspector, timeline), a start page and a library, with
empty panels, served at **`/app/`**. The old editor at `/` and `/compose.html` is unchanged.

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

## Caching

Vite content-hashes everything under `/app/assets/`, so those files are served `public, max-age=31536000,
immutable`. `/app/index.html` (which names the hashes) and the old pages stay `no-store`.

## Packaging

The wheel and sdist must contain `split_video/editor/static_app/`: pip users have no Node. `uv_build` packs
gitignored files under the module root, so running `npm ci && npm run build` in `frontend/` before `uv build` is all
that is needed. Check it with `uv build --out-dir dist && python3 scripts/check_wheel_contents.py dist`. The
repository has no job that publishes the wheel or sdist yet (a `v*.*.*` tag only publishes the Docker image); the
job that does must run the build first, as the `frontend` CI job does.
