# 0004: Frontend stack and structure

Status: accepted (2026-10-25). Board card: SPL-58. Refines 0002 (the "Frontend" bullet) and builds on 0003.
Amended 2026-10-25 (SPL-62): the Routing section now says the hash selects one of four views, not only the project id.
UX contract: [docs/ux-spec.md](../ux-spec.md).

## Context

[0002](0002-agent-first-hosted-ready.md) chose "Svelte 5, Vite and TypeScript, built to static assets and served by
FastAPI, client generated from `openapi.json`" in one line. [0003](0003-one-editor-one-project-model.md) then
turned the planned two ports (split page, compose page) into one editor. The M3 cards need the details settled
before any scaffold lands: layout of the repo, build and packaging, state, routing, testing, accessibility.

The current frontend (checked against the code, 2026-10-25):

- `src/split_video/editor/static/` holds two vanilla JS pages with no build step: `index.html` with `main.js`,
  `state.js`, `api.js`, `player.js`, `timeline.js`, `segmentTable.js`, `shortcuts.js`, `exportModal.js` and others
  (the split editor), and `compose.html` with `compose.js` and `compose.css` (compose). About 18 files, no
  modules bundler, no types, no tests in a browser.
- `editor/app.py` mounts that directory last, as `StaticFiles(directory=STATIC_DIR, html=True)` at `/`, so the
  catch-all cannot shadow `/api/*` or `/media/*`. A middleware sets `Cache-Control: no-store` on every path that is
  not under `/api/` or `/media/`, because the files have no content hashes. If the directory is empty at startup
  the app writes a placeholder `index.html` into it.
- The wheel is built by `uv_build` from `src/`, and the Docker image does `COPY src` then `uv sync`, so
  files under `src/split_video/editor/static/` ship as package data. The runtime image is `python:3.12-slim`
  plus ffmpeg and has no Node. `make dev` builds that image and runs `split-video edit /data` in a `--rm`
  container with only the video directory mounted.
- CI has `lint` (ruff plus four `--check` drift scripts for `openapi.json`, the edit plan and edit ops schemas
  and the error catalogue) and `test` (pytest). Dependabot covers `uv`, `github-actions` and `docker`.
- `openapi.json` is committed and drift-checked, and `/api/v1/events` is an SSE stream of job progress and
  project revision changes. Writes carry revisions (`If-Match`, `409` on a stale write).

## Decision

### Stack

- **Svelte 5 with runes** (`$state`, `$derived`, `$effect`), **Vite**, **strict TypeScript** (`"strict": true`,
  `noUncheckedIndexedAccess`). Svelte without SvelteKit: this is a single-page app served as static files by
  FastAPI, so SvelteKit's server routing, adapters and SSR have nothing to do here.
- **One shell, not pages.** The app is one component tree with the ux-spec regions: top bar, tab rail and left
  panel, preview, inspector and timeline, plus dialogs (export, shortcuts, first run) as overlays. There is no
  split page and no compose page.
- **No UI kit.** Components are our own. A large kit (Material, Skeleton, shadcn ports) brings its own look, which
  the ux-spec already specifies, and its own accessibility model to audit. Headless primitives are allowed one at
  a time, by a PR that justifies them (for example a focus-trapped dialog if the native `<dialog>` falls short).
  The timeline is custom either way: nothing off the shelf handles hours-long virtualised lanes.

### Folder layout

`frontend/` at the repo root, a standalone npm project (not inside `src/`, so Python packaging and `uv` never see
`node_modules`):

```
frontend/
  package.json, package-lock.json, .nvmrc
  vite.config.ts, svelte.config.js, tsconfig.json, eslint config
  index.html                  Vite entry
  src/
    main.ts                   mounts <App/>
    App.svelte                the shell: regions and overlays only
    lib/
      api/                    generated client (see below) + client.ts (auth header, If-Match, error mapping)
      state/                  runes stores: project, selection, playhead, jobs, ui (see State)
      shell/                  top bar, tab rail, panel and inspector frames, region resizing
      tabs/                   media/, highlights/, transcript/, text/, captions/, audio/, effects/
      inspector/              one component per selection kind (ux-spec #inspector)
      timeline/               lanes, ruler, playhead, snapping, virtualised drawing
      preview/                player and overlays
      export/                 export dialog and render queue
      firstrun/               start page and goal chooser
      library/                the library view
      a11y/                   shortcut registry, focus helpers, live region
      styles/                 tokens.css, base.css
    test/                     Vitest setup and fixtures
  e2e/                        Playwright specs (added in SPL-67)
```

`lib/` modules do not import from each other sideways except through `state/` and `api/`; `tabs/*` and
`inspector/*` are leaves. A module that needs the engine's rules calls the API: the frontend holds layout and
interaction, never editing rules (0003: the engine holds the rules).

### Build output and serving

- **Built output goes to `src/split_video/editor/static_app/`** (name chosen so the old `static/` is untouched),
  gitignored, **not committed**. Committing build output makes every UI PR a megabyte of minified diff and
  invites drift; the repo already has a drift-check culture for generated *sources of truth*, and build output is
  not one.
- **Built in CI and in the Docker build stage.** A new first stage `FROM node:<lts>-slim@sha256:<digest> AS frontend`
  runs `npm ci && npm run build` and the runtime stage does
  `COPY --from=frontend /frontend/dist ./src/split_video/editor/static_app`. Node never reaches the runtime
  image, which stays `python:3.12-slim` plus ffmpeg. The Node image is digest-pinned and bumped by Dependabot, like
  the Python base.
- **Served under `/app/`** by a second `StaticFiles` mount in `editor/app.py`, registered before the `/` catch-all
  (so `/app/` is not shadowed) with `html=True`, and Vite `base: "/app/"`. Why `/app/`:
  - the old pages keep `/` and `/compose.html` byte for byte, so nothing existing changes or breaks until
    cut-over;
  - it does not collide with `/api/*` or `/media/*`, and is short enough to type;
  - the app has no client routes (below), so no history-fallback handler is needed, only `html=True`.
  If `static_app/` is missing (a plain `uv run split-video edit` in a checkout with no frontend build), the mount
  is skipped and `/app/` returns 404 with the hint to run `npm run build`; the old pages work as before. The app
  must not write a placeholder into the directory the way the old mount does.
- **Caching.** Vite emits content-hashed files under `/app/assets/`. The `no-store` middleware gets one
  exception: `/app/assets/*` is `public, max-age=31536000, immutable`; `/app/index.html` stays `no-store`. The
  reason for blanket `no-store` (unhashed files) does not apply to hashed ones, and the timeline bundle is large
  enough that re-fetching it on every reload is felt.
- **Dev loop.** `npm run dev` runs Vite on `:5173` with a dev proxy for `/api` and `/media` (including SSE, no
  buffering) to `http://127.0.0.1:8765`, where `split-video edit` runs as usual. HMR works, no CORS config is
  needed on FastAPI, and the browser sees one origin. `make dev` keeps meaning "build the Docker image and run
  it": the image now contains the built app, and `/app/` is available there. A `make frontend` (build) and
  `make frontend-dev` (Vite) are added for non-Docker work. `make fetch-model` is unchanged.
- **Cut-over.** When the new editor covers the ux-spec M3 scope and passes the e2e suite: (1) `/` serves the new
  app (the mount moves from `/app/` to `/`, and `/app/` redirects to `/`); (2) `compose.html` and the old
  `index.html` redirect to it for one release; (3) the old `static/` files and the unversioned `/api` aliases
  that only they used are removed in a later release, with the removal listed in the changelog. Until step 1 the
  old pages are not edited except for bug fixes. A top-bar link "Try the new editor" on the old pages is allowed
  from the first usable build.

### Packaging for pip and uv users

The wheel must contain the built app, because pip users get no Node. Decision:

- The release build runs `npm ci && npm run build` before `uv build`, so `static_app/` is inside
  `src/split_video/editor/` when `uv_build` packs it. A release CI job (on `v*.*.*` tags) does this and uploads
  the wheel and sdist. The sdist carries the built `static_app/` too, so a source install does not need Node.
- A test (in the packaging card) opens the built wheel and asserts `split_video/editor/static_app/index.html`
  and at least one hashed asset are present. Without it the failure mode is a wheel that serves 404 at `/app/`.
- Developers who run from a checkout without Node keep the old pages; `/app/` says how to build. After cut-over a
  checkout needs `npm run build` once (documented in CONTRIBUTING and the Makefile), and the missing-build
  message becomes the error page at `/`.
- `uv_build` must be checked to include gitignored files under the module root. If it does not, the release job
  builds from a tree where `static_app/` is force-included through `[tool.uv.build-backend]` data settings. This
  is verified by the wheel test above, not assumed.

### State

- **Server is the source of truth; the browser keeps a mirror.** One `projectStore` (a Svelte 5 class with
  `$state` fields in `lib/state/project.svelte.ts`) holds the current timeline project and the **server revision**
  it was read at. Selection, playhead, zoom, active tab and region sizes are separate small stores; they are not
  part of the project and never sent to the server. Region sizes and the selected tab persist in `localStorage`
  (ux-spec: remembered per browser), wrapped in try/catch.
- **Edits are optimistic and revisioned.** An edit applies locally at once, is sent with `If-Match: <revision>`,
  and on success replaces the mirror with the server's returned project and new revision. On `409` the store
  fetches the current project, rebases or discards the local edit, and shows the conflict (error catalogue code,
  fix hint). Writes are serialised per project (one in flight, the rest coalesced) so a drag does not queue
  hundreds of requests.
- **Undo and redo are client-side and session-only** (ux-spec decision 4): a stack of inverse operations over
  the same edit API, shared by UI and agent edits (an agent edit arrives as a project change and is pushed on the
  stack with its label).
- **SSE drives refresh.** One `EventSource` on `/api/v1/events` (reconnect with backoff; a visible "reconnecting"
  state; on reconnect, refetch the project and compare revisions). A revision event for the open project with a
  revision above the mirror's triggers a refetch; job events update the `jobs` store (progress, cancel, result)
  that the queue, quick actions and top bar read. The browser never polls.
- Derived values (laid-out clip positions, visible ranges, selection summaries) are `$derived`, not stored.
  `$effect` is for I/O and DOM only, never for copying state.

### Routing

**No router library.** The app has four views, chosen by the **hash** and nothing else, in `lib/state/route.ts`
(parse and format, unit-tested) and `lib/state/route.svelte.ts` (a runes store that follows `hashchange`):

| Hash | View |
|---|---|
| `#/` (or anything unknown) | start: the first-run entry page |
| `#/library` and `#/library/<folder>` | the library: folders and videos under the server's root |
| `#/p/<project id>` | the editor with that project open; reading it at start is what makes a reload reopen it |
| `#/dev` | the components gallery (not linked) |

The project id is the video's path in the library (`POST /api/v1/open` takes it; ADR 0003's timeline project is
opened through it today), percent-encoded per path segment. Only the router store reads `location`; components read
`router.current` and build links with `hrefTo(...)`. The export dialog, shortcut help and first-run steps stay
state, not routes (`ui.exportOpen`, `ui.helpOpen`). Selected tab and region sizes stay out of the URL (they persist in
`localStorage`). Why not a library: nothing to guard, nested or lazy-load, and a router adds a dependency and a
history-fallback requirement on FastAPI; if real routes appear later (hosted, multiple projects) the change stays in
`lib/state/`.

### API client

- **Generated from the committed `openapi.json`** with **`@hey-api/openapi-ts`** using its `fetch` client (typed
  SDK functions and types, no runtime dependency beyond the thin client, tree-shakeable). `openapi-typescript`
  plus `openapi-fetch` was the close second (types only, hand-written calls); the full SDK generator was chosen
  because the same names then appear in agents' docs and the UI, and fewer hand-written wrappers can drift from
  the schema. If the generator proves unstable, falling back to `openapi-typescript` changes only `lib/api/`.
- **Generated code is committed** under `frontend/src/lib/api/generated/` and **drift-checked in CI**, the same
  pattern as the `scripts/export_*.py --check` jobs: `npm run api:generate && git diff --exit-code`. Committing
  it makes API changes visible in the same PR as the route change (review sees the type diff) and lets
  `svelte-check` and Vitest run without a Python step. The input is only `openapi.json`, so the frontend job needs
  no Python.
- Hand-written `lib/api/client.ts` sets the base URL, the optional bearer token, `If-Match` from the revision,
  maps the structured error body (`docs/errors.md` codes) to a typed error with the fix hint, and exposes SSE.
  Components never call `fetch`.
- A change to routes already requires `make openapi`; it now also requires `npm run api:generate`. CLAUDE.md and
  CONTRIBUTING get that line in the scaffold card.

### Design tokens and CSS

- **Plain CSS custom properties** in `styles/tokens.css` (colour, spacing, radii, type scale, z-index, region
  sizes from ux-spec #layout, focus ring), consumed through Svelte's scoped component styles. No Tailwind, no CSS
  framework, no CSS-in-JS. Why: the visual design is small (one dark theme, one accent, a fixed set of regions),
  scoped styles remove naming problems, tokens keep the theme in one file, and there is no extra build plugin or
  class-name vocabulary to teach agents and contributors. Dark only (ux-spec decision 10), so there is no theme
  switching layer; colours are still tokens so a second theme stays possible.
- Contrast rules (ux-spec #accessibility) are checked on the token pairs by a Vitest test that computes ratios, so
  a token change that breaks AA fails CI.
- Icons are inline SVG components in `lib/shell/icons/`, our own, no icon font and no third-party imagery.

### Accessibility and shortcuts

- **Native elements first** (`button`, `dialog`, `input`, `details`, landmarks `header`, `nav`, `main`, `aside`).
  ARIA only where no native element exists: tab rail (`tablist`/`tab`/`tabpanel`), timeline lanes
  (`listbox`/`option` per lane with a roving tabindex, no `application` role), progress bars (`progressbar` with value).
- **svelte-check's a11y warnings are errors in CI**, and `eslint-plugin-svelte` a11y rules are on. Playwright
  specs (SPL-67) run `@axe-core/playwright` on the first-run page, the shell with each tab, and the export
  dialog, and fail on serious or critical violations.
- **One shortcut registry** in `lib/a11y/shortcuts.ts`: each shortcut is data (`keys`, `action id`, `label`,
  `when`, `group`). One `keydown` listener on `window` dispatches from the registry, skips text fields except
  Escape and Ctrl/Cmd combinations, and maps Ctrl to Cmd on macOS. Tooltips and the `?` overlay are generated
  from the same registry, so the overlay "lists exactly the table" in ux-spec #shortcuts and cannot drift. A
  Vitest test compares the registry to the ux-spec table (parsed from the markdown) so the doc and code agree.
- **Focus management** is centralised: region order from ux-spec #accessibility, a focus ring from a token,
  focus trapped in dialogs and returned on close, a polite live region (`lib/a11y/announce.ts`) for errors, job
  completion and agent edits. Motion respects `prefers-reduced-motion`.

### Testing and CI

- **Vitest** (with `@testing-library/svelte` and jsdom) for stores, the API client's error and revision handling,
  snapping and timeline maths, the shortcut registry and token contrast. Pure logic is kept out of components so
  it tests without a DOM. No snapshot tests of markup.
- **Playwright** for end-to-end, added in SPL-67: it starts the real FastAPI app on a free port against a tiny
  generated video, in the background with a hard deadline, and kills it by PID. Specs cover first run, the
  timeline interactions, agent edits arriving over SSE and a11y checks. Real mouse drag on split markers does not
  complete under Playwright (SPL-102), so drag specs dispatch pointer events directly.
- **CI**: a new `frontend` job on `ubuntu-latest`, `timeout-minutes: 10`, with Node from `.nvmrc` and the npm
  cache, running `npm ci`, `npm run lint` (eslint and prettier `--check`), `npm run check` (`svelte-check
  --fail-on-warnings`), `npm run test` (Vitest), `npm run api:generate` plus `git diff --exit-code`, and
  `npm run build`. It runs in parallel with `lint` and `test`. The existing `lint` and `test` jobs stay as they
  are; the Docker build in `build-and-push` already exercises the Node stage. Branch protection adds `frontend`
  as a required check once the job exists (a repo setting, done by the owner, not by the PR). The Playwright job
  is a separate `e2e` job added by SPL-67, needing `ffmpeg` and the built app.
- The `.githooks/pre-push` gate gains the cheap frontend checks (`lint`, `check`) only when `frontend/node_modules`
  exists, so Python-only contributors are not blocked.

### Dependencies

- **Small and pinned.** Runtime dependencies are `svelte` and the generated client's runtime only. Everything else
  is a devDependency (vite, `@sveltejs/vite-plugin-svelte`, typescript, svelte-check, vitest, eslint and
  prettier with their svelte plugins, playwright, axe, the generator). Exact versions in `package.json` (no `^`),
  `package-lock.json` committed, `npm ci` everywhere, Node version in `.nvmrc` (an LTS) and the Docker stage.
- **A new dependency needs a reason in its PR** (what it replaces, size, maintenance). No state library, router,
  CSS framework, component kit, date library or utility library (lodash and similar) in M3.
- **Dependabot gets an `npm` ecosystem entry** for `/frontend`, weekly, with minor and patch updates of dev
  tooling grouped into one PR to keep the noise down; `svelte`, `vite` and `typescript` majors stay separate. The
  `docker` entry already covers the Node base image digest. `npm audit --omit=dev` is added to the existing audit
  workflow.

## Consequences

- A Node toolchain enters the repo (as 0002 anticipated). It is confined to `frontend/`, one CI job and one
  Docker stage; the runtime image does not grow, and Python-only work is unaffected until cut-over.
- Docker build time rises by the `npm ci` and Vite build (cached by layer when `frontend/package*.json` is
  unchanged: copy the manifests, run `npm ci`, then copy sources).
- Every route change now has two generated artefacts to refresh (`openapi.json`, the TypeScript client); CI fails
  on either drift, so reviewers see the UI-facing effect.
- A checkout run without a frontend build serves the old pages only, so the first weeks of M3 look the same to
  anyone who does not build the app. After cut-over, contributors who touch only Python need to run the build once
  (or use the Docker image) to see the UI.
- pip and uv users depend on the release job building the app before `uv build`; the wheel test guards it.
- Two UIs coexist for a period, so shared behaviour (the engine and `/api/v1`) must not change shape for either
  (already the rule for public surfaces). Bug fixes in the old pages are allowed; features are not.
- `no-store` for everything but hashed assets keeps the stale-asset protection that the existing middleware
  documents, because only content-hashed files become cacheable.
- No router means no deep links beyond the project hash until a hosted mode needs them.

## Alternatives considered

- **SvelteKit.** Adds file-based routing, an adapter, and SSR we do not use, and its static adapter still needs
  a fallback page configuration on FastAPI. A Vite SPA is the smaller fit; moving to SvelteKit later is possible
  because `lib/` has no framework-router dependency.
- **Commit the built assets** (as the old static files are). Zero-Node installs from git, but large noisy diffs,
  merge conflicts on hashed files and an easy-to-forget rebuild. The Docker stage and release job give the same
  result without it.
- **Build into `static/` and replace the old pages in place.** No coexistence: the first merge would break the
  working editor before the new one is usable, against the "old pages untouched until replaced" order of work.
- **Serve the new app at `/` and move the old pages to `/legacy/`.** Changes the URL everyone uses on day one.
  `/app/` costs one mount and flips later.
- **A client-side router (svelte-spa-router, page.js).** Nothing to route to in a single-shell, single-user app.
- **Redux-style or third-party state (zustand-like stores, TanStack Query).** The server revision already gives
  the cache-validity rule and Svelte 5 runes cover local state; a query cache would duplicate revision handling
  and fight the SSE-driven refresh.
- **Hand-written API types and `fetch` calls.** Drifts from `openapi.json`, which is the contract agents already
  use. **Generating at build time only** keeps the repo smaller but hides API effects from review and makes
  `svelte-check` depend on a build step.
- **Tailwind or a component kit (Material, Skeleton, Bits UI as a whole).** Faster first screens, but a second
  design vocabulary and a third-party accessibility surface to audit, for a UI whose look is already specified.
  The existing `material-design-3` skill is not used: the ux-spec sets a CapCut-style dark layout of our own.
- **Web components or React.** Svelte 5 was settled in 0002; this ADR does not reopen it.
- **Cypress or Selenium for e2e.** Playwright is already how browser checks are done on this project, with known
  quirks recorded (SPL-102).

## Out of scope

Code and the scaffold itself (a card per step: scaffold and CI, client generation, shell, then the tabs), the
visual design (ux-spec), the browser compositor decision (0002 spike), hosting and its routing, internationalisation,
touch support, and a light theme.
