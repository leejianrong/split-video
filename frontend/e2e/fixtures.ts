import { test as base, expect, type Page } from '@playwright/test';
import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { GIG_PATH } from './global-setup';
import { GIG_NAME } from './media';

const REPO = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
// `uv run` finds the project's virtualenv (CI runs `uv sync --frozen` first). Override for a different launcher.
const LAUNCHER = (process.env.E2E_SPLIT_VIDEO ?? 'uv run --frozen split-video').split(' ');

export interface App {
  /** `http://127.0.0.1:<port>`; the app is at `/app/`. */
  baseURL: string;
  /** The workspace the server was started on (a temp directory, deleted after the test). */
  dir: string;
  /** Path of the copied recording in the workspace (absent when `seedMedia` is false). */
  video: string;
  /** Run a `split-video` CLI command against the workspace, as an outside agent would. Returns stdout. */
  cli(args: string[]): string;
  /** Apply timeline ops from outside the browser (the CLI `apply-ops`). */
  applyOps(ops: object[]): string;
  /** The server's combined log, for failure messages. */
  log(): string;
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address() as net.AddressInfo;
      srv.close(() => resolve(port));
    });
  });
}

async function waitForServer(base: string, child: ChildProcess, log: () => string): Promise<void> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null)
      throw new Error(`server exited early (${child.exitCode}):\n${log()}`);
    try {
      const r = await fetch(`${base}/api/v1/session`, { signal: AbortSignal.timeout(2_000) });
      if (r.ok) return;
    } catch {
      // not listening yet
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`server did not start within 60 s:\n${log()}`);
}

/** Kill the whole process group (the `uv run` wrapper and the Python server under it), then wait for it to exit. */
async function stop(child: ChildProcess): Promise<void> {
  if (child.pid === undefined || child.exitCode !== null) return;
  const exited = new Promise<void>((r) => child.once('exit', () => r()));
  try {
    process.kill(-child.pid, 'SIGTERM');
  } catch {
    return;
  }
  const timer = setTimeout(() => {
    try {
      process.kill(-child.pid!, 'SIGKILL');
    } catch {
      // already gone
    }
  }, 5_000);
  await exited;
  clearTimeout(timer);
}

export const test = base.extend<{ app: App; seedMedia: boolean }, object>({
  seedMedia: [true, { option: true }],
  app: async ({ seedMedia }, use, testInfo) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sv-e2e-'));
    const video = path.join(dir, GIG_NAME);
    if (seedMedia) fs.copyFileSync(GIG_PATH, video);
    const port = await freePort();
    const base = `http://127.0.0.1:${port}`;
    let out = '';
    const [cmd = 'uv', ...pre] = LAUNCHER;
    // The workspace form (`edit <dir>`) starts on the library; a project is opened from the browser by hash.
    const child = spawn(cmd, [...pre, 'edit', dir, '--port', String(port), '--no-browser'], {
      cwd: REPO,
      detached: true, // its own process group, so teardown reaches the Python process under `uv run`
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    child.stdout?.on('data', (d) => (out += d));
    child.stderr?.on('data', (d) => (out += d));
    const log = () => out.slice(-4000);
    try {
      await waitForServer(base, child, log);
      const cli = (args: string[]) =>
        execFileSync(cmd, [...pre, ...args], {
          cwd: REPO,
          timeout: 60_000,
          stdio: ['ignore', 'pipe', 'pipe'],
        }).toString();
      const app: App = {
        baseURL: base,
        dir,
        video,
        cli,
        applyOps: (ops) => {
          const file = path.join(
            dir,
            `.ops-${Date.now()}-${Math.random().toString(36).slice(2)}.json`,
          );
          fs.writeFileSync(file, JSON.stringify({ schema_version: 1, ops }));
          return cli(['apply-ops', file, '--video', video, '--root', dir]);
        },
        log,
      };
      await use(app);
    } finally {
      if (testInfo.status !== testInfo.expectedStatus) {
        await testInfo.attach('server.log', { body: out, contentType: 'text/plain' });
      }
      await stop(child);
      fs.rmSync(dir, { recursive: true, force: true });
    }
  },
});

export { expect };

interface ClipJson {
  id: string;
  asset: string;
  in_point: number;
  out_point: number;
  start: number;
  end: number;
  gain: number;
  label: string;
  crop: { x: number; y: number; w: number; h: number } | null;
}
export interface TimelineJson {
  revision: number;
  duration: number;
  tracks: { id: string; kind: string; clips: ClipJson[] }[];
}

/** The recording's timeline as the server holds it (`GET /project/timeline`): the truth a UI action must have reached. */
export async function timeline(app: App, name = 'gig.mp4'): Promise<TimelineJson> {
  const r = await fetch(`${app.baseURL}/api/v1/project/timeline?path=${encodeURIComponent(name)}`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!r.ok) throw new Error(`timeline ${r.status}: ${await r.text()}`);
  return (await r.json()) as TimelineJson;
}

/** Open the editor on the workspace's recording and wait until its clips are on the timeline. */
export async function openEditor(page: Page, app: App) {
  await openApp(page, app, '#/p/gig.mp4');
  await page.getByTestId('clip').first().waitFor();
}

/** Open a path under the app, e.g. `open(page, app, '#/p/gig.mp4')`. */
export async function openApp(page: Page, app: App, hash = '#/') {
  await page.goto(`${app.baseURL}/app/${hash}`);
}

/**
 * Run a Media tab quick action and wait for it to finish. Fails at once, with the message the user would read, when
 * the run ends in an error, instead of timing out on a state that will never come.
 */
export async function runQuickAction(page: Page, action: string, timeout = 90_000) {
  await page.locator(`button[data-action=${action}]`).click();
  const run = page.locator(`[data-testid=run][data-action=${action}]`);
  await expect
    .poll(async () => await run.getAttribute('data-state'), {
      timeout,
      message: `${action} run state`,
    })
    .toMatch(/^(done|error)$/);
  expect(await run.getAttribute('data-state'), await run.innerText()).toBe('done');
  return run;
}
