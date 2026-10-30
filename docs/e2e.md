# Browser end-to-end tests

Playwright tests drive the built editor (`/app/`) in Chromium against a real `split-video edit` server. They live in
`frontend/e2e/` and run in CI as the `e2e` job on every pull request ([ADR 0004](adr/0004-frontend-stack-and-structure.md#testing-and-ci)).
(The agent end-to-end walk-through in [e2e-agent-test.md](e2e-agent-test.md) is a different thing: it tests the CLI and MCP
surfaces, not the browser.)

## What is covered

| Spec | Flow |
|---|---|
| `first-run.spec.ts` | Import a recording on the first-run page, "Split into songs" finds three songs, the editor opens on the Media tab with three labelled segments; a file that is not a video reports an error with a fix |
| `media-tab.spec.ts` | Import a second recording from the Media tab, drag its card to the timeline, and the add button |
| `highlights.spec.ts` | Find highlights, review the cards, Make short (a labelled, cropped clip lands on the timeline), Dismiss |
| `inspector.spec.ts` | Change a clip's volume (saved, survives a reload); an out-of-range value is refused and nothing is saved |
| `export.spec.ts` | Render the timeline to a tiny 720p file from the export dialog and check it with `ffprobe`; an existing file fails with a fix |
| `live-refresh.spec.ts` | An outside CLI edit (`apply-ops`) appears in the open editor without a reload; removing the selected clip clears the selection |
| `a11y.spec.ts` | axe checks of the first-run page, the library, the shell on every tab, the inspector and the export dialog |

Not covered: visual regression (no screenshot comparison), and the old pages at `/` and `/compose.html`.

## How it works

- **Media is generated, never committed.** `e2e/global-setup.ts` runs ffmpeg (`lavfi`, with `-nostdin` and a timeout)
  once per run into `frontend/e2e-results/media/`: `gig.mp4` is 108 s with three 32 s tones separated by 4 s of silence
  (about 6 MB), and `extra.mp4` is a different 8 s clip for import tests.
- **One server per test.** The `app` fixture (`e2e/fixtures.ts`) copies the recording into a fresh temp workspace,
  starts `uv run split-video edit <dir> --port <free port> --no-browser` in its own process group, waits for
  `/api/v1/session`, and on teardown kills the whole group (the `uv run` wrapper and the Python server) and deletes the
  workspace. Tests share nothing, so they run in parallel and in any order. `test.use({ seedMedia: false })` starts on
  an empty workspace (first run). `app.applyOps()` runs the real CLI from outside the browser, as an agent would.
- **Assert on the server's state, not only on pixels.** After a UI action the tests read `GET /api/v1/project/timeline`
  (the `timeline(app)` helper) to prove the edit reached the engine.
- **No fixed sleeps.** Wait for a state (`expect(...).toHaveAttribute`, `expect.poll`), never `waitForTimeout`.
- **Drag.** Dragging a Media card onto the timeline works with `dragTo`. A real mouse drag on split markers does not
  complete under Playwright (SPL-102): dispatch pointer events for those.
- **Hard limits.** 90 s per test, a 12 minute global timeout in CI, a 20 minute job timeout, and a 60 s server start
  deadline. A wedged ffmpeg or server fails a test; it cannot hold a runner (SPL-110).

## Running it locally

```bash
make frontend                      # the tests run against the build, so rebuild after changing the UI
cd frontend
npx playwright install chromium    # once (add --with-deps on a fresh Linux machine)
npm run e2e                        # all specs, 4 workers
npx playwright test inspector      # one spec
npx playwright test --ui           # the Playwright UI; `npx playwright show-trace <trace.zip>` for a failure
```

It needs `ffmpeg` and `ffprobe` on the path and a synced Python environment (`uv sync`). To use another launcher for
the server, set `E2E_SPLIT_VIDEO` (default `uv run --frozen split-video`), for example `E2E_SPLIT_VIDEO=".venv/bin/split-video"`.
The YAMNet model is not needed (CI does not have it): the song and highlight flows use silence and loudness only.
A full run takes under a minute on 4 workers; CI runs 2 and takes a few minutes.

## In CI

The `e2e` job (`.github/workflows/ci.yml`) installs ffmpeg, uv and Node, builds the frontend, restores the Chromium
download from the cache (keyed by `package-lock.json`, so a Playwright bump refreshes it), installs the browser and its
system libraries (`npx playwright install --with-deps chromium`), and runs `npm run e2e`. On failure it uploads the
`e2e-report` artifact (HTML report, and per failed test the trace, screenshot and the server's log).
Open a trace with `npx playwright show-trace trace.zip` or at trace.playwright.dev.

`e2e` should be a required check on `main` (a repository setting, like `test`, `lint` and `frontend`).

## Accessibility checks

`a11y.spec.ts` runs axe (`@axe-core/playwright`, WCAG 2 A and AA tags) and fails on **serious or critical** violations.
Moderate and minor ones are not asserted. Known exceptions are listed in code, with the reason, and must stay few:

- `.disabled output` is excluded: the value readout of a disabled slider is dimmed on purpose, which WCAG 1.4.3 allows
  for inactive controls, but axe cannot tell that the `<output>` belongs to a disabled control.

The first run of these checks found two real problems, fixed in the same PR: the Media card and the Highlight card
buttons had an `aria-label` that did not start with their visible text (WCAG 2.5.3, Label in Name).

## Flaky-test policy

A flaky test is worse than no test: people learn to re-run red builds. The rules:

1. **One retry, configured, nowhere else.** `playwright.config.ts` sets `retries: 1` in CI and 0 locally. Do not
   re-run a red `e2e` job to "see if it goes green", and do not add `test.retry`, loops or `--repeat-each` workarounds
   to a spec. A test that passes only on the retry is reported as **flaky** in the log and the PR annotations: treat that
   as a bug in the test or in the app, not as a pass.
2. **Fix first.** Most flakes are a missing wait. Replace sleeps with a wait on a visible state or a server value, give
   each test its own data (the fixture already does), and read the trace before guessing.
3. **Quarantine if it cannot be fixed this week.** Add the tag and a link, in the same PR:

   ```ts
   test('what it checks @quarantine', async ({ page, app }) => {
     test.info().annotations.push({ type: 'issue', description: 'SPL-123' });
   ```

   Quarantined tests are skipped by `npm run e2e` (so they cannot block anyone) and run in a separate, non-blocking step
   (`npm run e2e:quarantine`), so their failures stay visible in the job log.
4. **One issue per quarantined test**, on the Pandan board, with the failing trace attached and an owner. The issue number is
   in the test's annotation. A quarantined test is fixed or deleted within two weeks; the list is reviewed whenever
   someone touches `frontend/e2e/`. `grep -rn "@quarantine" frontend/e2e` is the list.
5. **A genuine product bug is not a flake.** If a test fails because the app is racy, that is a bug in the app: file it,
   and quarantine the test only while it is open.
6. Never raise a timeout to hide a flake without finding why the thing is slow.
