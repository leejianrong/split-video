import { defineConfig, devices } from '@playwright/test';

// Browser end-to-end tests (docs/e2e.md). Each test starts its own `split-video edit` server on a free port against a
// temporary workspace (e2e/fixtures.ts), so specs share nothing and run in parallel.
const inCI = !!process.env.CI;
const quarantine = !!process.env.E2E_QUARANTINE;
// The quarantine run writes elsewhere: Playwright empties its output folders at the start of a run.
const results = quarantine ? 'e2e-results/quarantine' : 'e2e-results';

export default defineConfig({
  testDir: 'e2e',
  globalSetup: './e2e/global-setup.ts',
  outputDir: `${results}/artifacts`,
  // Hard limits at every level: a wedged server or ffmpeg must fail a test, never hold a runner (SPL-110).
  timeout: 90_000,
  expect: { timeout: 15_000 },
  globalTimeout: inCI ? 12 * 60_000 : 20 * 60_000,
  // One retry in CI, none locally. A test that needs the retry is reported as flaky and must be quarantined or
  // fixed (docs/e2e.md); there is no other retry anywhere in the suite.
  retries: inCI ? 1 : 0,
  workers: inCI ? 2 : 4,
  fullyParallel: true,
  forbidOnly: inCI,
  reporter:
    inCI && !quarantine
      ? [['list'], ['html', { open: 'never', outputFolder: `${results}/report` }], ['github']]
      : [['list']],
  // `@quarantine` tests are skipped by `npm run e2e` and run by `npm run e2e:quarantine` (a non-blocking CI step).
  grepInvert: quarantine ? undefined : /@quarantine/,
  grep: quarantine ? /@quarantine/ : undefined,
  use: {
    ...devices['Desktop Chrome'],
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
});
