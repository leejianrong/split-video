import type { Page } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { test, expect, openEditor, timeline, type App } from './fixtures';

// Playback through pre-rendered lookahead chunks (SPL-72): the composite plays in a <video>, chunks join without a gap,
// a seek and an edit recover, a pause cancels the renders still running. Numbers are printed as `PLAYBACK ...` lines.
// The join is judged on the picture's own time, which is exact: the timeline time of consecutive shown frames is one
// frame (33 ms) apart, a dropped frame makes it two or more. On a quiet machine a join measures 33 ms; the bound is
// four frames less a margin because a loaded runner stalls the page for whole display refreshes, and because the swap
// hides the old element before its last frame's callback runs, so that frame is sometimes not seen (a 67 ms step with
// the picture still continuous). Before SPL-126 the step was 133 to 167 ms on every run. The wall-clock gap is printed
// but not asserted: it is quantised to the 16.7 ms refresh and a stall anywhere shows in it.
const JOIN_STEP_MS = 120;
const SHOTS = process.env.PLAYBACK_SHOTS; // a folder: also take the screenshots the card's verification reads

const seconds = async (page: Page): Promise<number> => {
  const text = (await page.getByTestId('readout').innerText()).split('/')[0]!.trim();
  return text.split(':').reduce((acc, part) => acc * 60 + Number(part), 0);
};
const frame = (page: Page) => page.getByTestId('preview-frame');
const showing = (page: Page, what: string, timeout = 20_000) =>
  expect(frame(page)).toHaveAttribute('data-showing', what, { timeout });
// The draft tests queue the chunk behind six held renders, which take a minute on a loaded runner: the hand-over
// waits that long (a chunk that never arrives still fails), the draft itself is still asserted within 20 s.
const HANDOVER_MS = 60_000;

async function previewJobs(app: App) {
  const r = await fetch(`${app.baseURL}/api/v1/jobs`, { signal: AbortSignal.timeout(10_000) });
  const body = (await r.json()) as { jobs: { kind: string; state: string; label: string }[] };
  return body.jobs.filter((j) => j.kind === 'preview_range');
}

async function shot(page: Page, name: string) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  for (const [w, h] of [
    [1440, 900],
    [1280, 720],
  ] as const) {
    await page.setViewportSize({ width: w, height: h });
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(SHOTS, `${name}-${w}x${h}.png`) });
    const boxes = await page.evaluate(() =>
      Object.fromEntries(
        ['top-bar', 'left', 'inspector', 'timeline', 'preview'].map((r) => {
          const b = document.querySelector(`[data-region=${r}]`)?.getBoundingClientRect();
          return [r, b ? [Math.round(b.width), Math.round(b.height)] : null];
        }),
      ),
    );
    console.log(`PLAYBACK boxes ${name} ${w}x${h} ${JSON.stringify(boxes)}`);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

test('playback: the composite plays through lookahead chunks, crosses a join, seeks, survives an edit and a pause', async ({
  page,
  app,
}) => {
  test.setTimeout(150_000);
  const errors: string[] = [];
  const starts: number[] = []; // the timeline second each chunk file starts at (lead-in included), in request order
  let settled = false;
  // A chunk 404s once the timeline moves past its revision, so a late fetch of an older chunk is not an error: only
  // the newest revision asked for counts (key -> revision from the start responses).
  const revisionOf = new Map<string, number>();
  let newest = 0;
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('response', async (r) => {
    const url = r.url();
    if (r.request().method() === 'POST' && /\/project\/preview-range\?/.test(url) && r.ok()) {
      const body = (await r.json().catch(() => null)) as { key: string; revision: number } | null;
      if (body) {
        revisionOf.set(body.key, body.revision);
        starts.push((r.request().postDataJSON() as { start: number }).start);
        newest = Math.max(newest, body.revision);
      }
      return;
    }
    // (the waveform of a fresh recording is a known 404 until it is computed)
    if (settled || r.status() < 400 || !/preview-|\/media\//.test(url)) return;
    const key = /\/preview-range\/([^/?]+)/.exec(url)?.[1];
    if (key && (revisionOf.get(key) ?? newest) < newest) return;
    errors.push(`${r.status()} ${url}`);
  });

  await page.addInitScript(() => {
    const w = window as unknown as { __blobs: string[] };
    w.__blobs = [];
    const create = URL.createObjectURL.bind(URL);
    URL.createObjectURL = (o: Blob | MediaSource) => {
      const url = create(o);
      if (o instanceof Blob && o.type === 'video/mp4') w.__blobs.push(url); // the chunks, in the order they were asked for
      return url;
    };
  });
  await openEditor(page, app);
  const codecs = await page.evaluate(() =>
    document.createElement('video').canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"'),
  );
  test.skip(codecs === '', 'this browser build cannot decode H.264 and AAC');

  // A picture-in-picture and a title over the first minute, with the pip's own sound.
  const clipsBefore = await page.getByTestId('clip').count();
  const first = (await timeline(app)).tracks[0]!.clips[0]!;
  app.applyOps([
    { op: 'add_track', id: 'ov', kind: 'video' },
    { op: 'add_clip', id: 'pip', asset: first.asset, track: 'ov', in: 40, out: 100, start: 0 },
    { op: 'set_clip_properties', clip: 'pip', scale: 0.4, x: 0.75, y: 0.25 },
    { op: 'add_track', id: 'ti', kind: 'video' },
    { op: 'add_text_clip', id: 'cap', track: 'ti', text: 'Wait for it', duration: 60, start: 0 },
  ]);
  await expect(page.getByTestId('clip')).toHaveCount(clipsBefore + 2, { timeout: 20_000 });
  await expect(page.getByTestId('preview-still')).toBeVisible();

  // Frame gaps: every frame presented on screen (by either element), by wall clock, with the element it came from.
  await page.evaluate(() => {
    const w = window as unknown as { __frames: [number, string, number, string][] };
    w.__frames = [];
    document.querySelectorAll<HTMLVideoElement>('[data-testid=preview-video]').forEach((v) => {
      const tick = (now: number, meta: VideoFrameCallbackMetadata) => {
        // a hidden element decodes ahead: only what is shown counts
        if (v.classList.contains('on'))
          w.__frames.push([now, v.dataset.slot!, meta.mediaTime, v.currentSrc]);
        v.requestVideoFrameCallback(tick);
      };
      v.requestVideoFrameCallback(tick);
    });
  });

  // Play: a draft or the composite starts, the composite takes over, the playhead keeps wall-clock pace.
  const t0 = Date.now();
  await page.keyboard.press('Space');
  await expect(frame(page)).not.toHaveAttribute('data-playback', 'paused');
  await expect(page.getByTestId('play')).toHaveAttribute('aria-pressed', 'true');
  await expect(
    page.locator('[data-testid=preview-draft-badge], [data-testid=preview-preparing]').first(),
  ).toBeVisible();
  await showing(page, 'chunk');
  const coldStart = Date.now() - t0;
  console.log(`PLAYBACK cold start to composite: ${coldStart} ms`);
  const playing = page.locator('[data-testid=preview-video].on');
  await expect(playing).toHaveCount(1);
  await shot(page, 'playing-chunk');
  // (resizing the window for the screenshots stalls frames, and so would polling: measure from here, then wait idle
  // across the 8 s join)
  await page.evaluate(() => ((window as unknown as { __frames: unknown[] }).__frames.length = 0));
  const p0 = await seconds(page);
  const w0 = Date.now();
  await page.waitForTimeout(11_000);
  const p1 = await seconds(page);
  expect(p0).toBeLessThan(8);
  expect(p1).toBeGreaterThan(8);
  const pace = (p1 - p0) / ((Date.now() - w0) / 1000);
  console.log(`PLAYBACK playhead pace across a join: ${pace.toFixed(2)}x`);
  expect(pace).toBeGreaterThan(0.8);
  expect(pace).toBeLessThan(1.2);
  await showing(page, 'chunk');

  const gaps = await page.evaluate((chunkStarts) => {
    const w = window as unknown as {
      __frames: [number, string, number, string][];
      __blobs: string[];
    };
    // timeline ms of each shown frame: where its file starts on the timeline plus its time in the file
    const f = w.__frames.map(([t, slot, media, src]) => ({
      t,
      slot,
      at: (chunkStarts[w.__blobs.indexOf(src)]! + media) * 1000,
    }));
    const d = f
      .slice(1)
      .map((x, i) => ({ gap: x.t - f[i]!.t, step: x.at - f[i]!.at, swap: x.slot !== f[i]!.slot }));
    const sorted = d.map((x) => x.gap).sort((a, b) => a - b);
    const joins = d.filter((x) => x.swap);
    return {
      frames: f.length,
      median: sorted[Math.floor(sorted.length / 2)]!,
      max: sorted[sorted.length - 1]!,
      swaps: joins.map((x) => x.gap),
      steps: joins.map((x) => x.step),
    };
  }, starts);
  console.log(
    `PLAYBACK frame gap: median ${gaps.median.toFixed(0)} ms, max ${gaps.max.toFixed(0)} ms, at the swap ${gaps.swaps.map((g) => g.toFixed(0)).join(', ')} ms (picture advances ${gaps.steps.map((g) => g.toFixed(0)).join(', ')} ms), over ${gaps.frames} frames`,
  );
  // Only the join is asserted: a one-off stall of a loaded runner elsewhere (the max is printed above) is not a
  // playback regression, a gap at the chunk swap is.
  expect(gaps.swaps.length).toBeGreaterThanOrEqual(1); // a join was crossed
  expect(Math.max(...gaps.steps)).toBeLessThan(JOIN_STEP_MS); // no frame dropped
  expect(Math.min(...gaps.steps)).toBeGreaterThan(0); // none shown twice or out of order

  // The element playing is the composite chunk, it decodes sound, and its picture is the composite (overlay and title).
  const audio = await playing.evaluate(
    (v) => (v as unknown as { webkitAudioDecodedByteCount?: number }).webkitAudioDecodedByteCount,
  );
  expect(audio ?? 1).toBeGreaterThan(0);
  await page.keyboard.press('k'); // K pauses too
  await expect(frame(page)).toHaveAttribute('data-playback', 'paused');
  const compared = await page.evaluate(async () => {
    const v = document.querySelector<HTMLVideoElement>('[data-testid=preview-video].on')!;
    const small = (src: CanvasImageSource) => {
      const c = document.createElement('canvas');
      c.width = 64;
      c.height = 48;
      const x = c.getContext('2d')!;
      x.drawImage(src, 0, 0, 64, 48);
      return x.getImageData(0, 0, 64, 48).data;
    };
    const chunkPixels = small(v);
    const readout = document
      .querySelector('[data-testid=readout]')!
      .textContent!.split('/')[0]!
      .trim()
      .split(':')
      .reduce((a, p) => a * 60 + Number(p), 0);
    return { chunkPixels: Array.from(chunkPixels), readout };
  });
  // Same instant as a composite still and as the plain main-track file.
  const still = await page.evaluate(
    async ([base, time, assetId]) => {
      const toPixels = async (src: string) => {
        const img = new Image();
        img.src = src;
        await img.decode();
        const c = document.createElement('canvas');
        c.width = 64;
        c.height = 48;
        const x = c.getContext('2d')!;
        x.drawImage(img, 0, 0, 64, 48);
        return Array.from(x.getImageData(0, 0, 64, 48).data);
      };
      const composite = await (
        await fetch(`${base}/api/v1/project/preview-frame?path=gig.mp4&time=${time}&width=320`)
      ).blob();
      const main = document.createElement('video');
      main.muted = true;
      main.src = `/api/v1/media/asset/${assetId}`;
      await new Promise((r) => main.addEventListener('loadeddata', r, { once: true }));
      main.currentTime = time;
      await new Promise((r) => main.addEventListener('seeked', r, { once: true }));
      const c = document.createElement('canvas');
      c.width = 64;
      c.height = 48;
      const x = c.getContext('2d')!;
      x.drawImage(main, 0, 0, 64, 48);
      return {
        composite: await toPixels(URL.createObjectURL(composite)),
        main: Array.from(x.getImageData(0, 0, 64, 48).data),
      };
    },
    [app.baseURL, compared.readout, first.asset] as const,
  );
  const mad = (a: number[], b: number[]) => {
    let s = 0;
    for (let i = 0; i < a.length; i += 4)
      s +=
        Math.abs(a[i]! - b[i]!) + Math.abs(a[i + 1]! - b[i + 1]!) + Math.abs(a[i + 2]! - b[i + 2]!);
    return s / (a.length / 4) / 3;
  };
  const toComposite = mad(compared.chunkPixels, still.composite);
  const toMain = mad(compared.chunkPixels, still.main);
  console.log(
    `PLAYBACK chunk frame vs composite still ${toComposite.toFixed(1)}, vs plain main frame ${toMain.toFixed(1)}`,
  );
  expect(toComposite).toBeLessThan(toMain); // the overlay and title are in the picture
  await shot(page, 'paused');

  // Seek during playback: the still shows at once, playback resumes within about 2 s (cold place, then a cached one).
  await page.keyboard.press('Space');
  await showing(page, 'chunk');
  const ruler = (await page.getByTestId('ruler').boundingBox())!;
  const seekTo = async (fraction: number) => {
    const s0 = Date.now();
    await page.mouse.click(ruler.x + ruler.width * fraction, ruler.y + ruler.height / 2);
    await expect(frame(page))
      .not.toHaveAttribute('data-showing', 'chunk', { timeout: 5_000 })
      .catch(() => undefined);
    await showing(page, 'chunk');
    return Date.now() - s0;
  };
  const cold = await seekTo(0.6);
  console.log(`PLAYBACK seek recovery, uncached place: ${cold} ms`);
  expect(cold).toBeLessThan(3_000);
  await shot(page, 'playing-after-seek');

  // Pause cancels what is still rendering, for real. Two long renders hold the server's one preview worker, so the
  // chunk playback asks for next is queued behind them when the pause comes.
  await page.keyboard.press('Space');
  await expect(frame(page)).toHaveAttribute('data-playback', 'paused');
  const hold = (height: number) =>
    fetch(`${app.baseURL}/api/v1/project/preview-range?path=gig.mp4`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ start: 30, end: 60, height }),
      signal: AbortSignal.timeout(10_000),
    });
  await hold(240);
  await hold(180);
  await page.mouse.click(ruler.x + ruler.width * 0.85, ruler.y + ruler.height / 2);
  const asked = page.waitForResponse((r) => r.url().includes('/project/preview-range?'));
  await page.keyboard.press('Space');
  await asked;
  await page.keyboard.press('Space');
  await expect(frame(page)).toHaveAttribute('data-playback', 'paused');
  await expect
    .poll(
      async () =>
        (await previewJobs(app)).filter((j) => j.label.includes('87-96') && j.state === 'cancelled')
          .length,
      { timeout: 10_000, message: 'the queued chunk job was cancelled' },
    )
    .toBe(1);
  await expect
    .poll(
      async () =>
        (await previewJobs(app)).filter((j) => j.state === 'queued' || j.state === 'running')
          .length,
      { timeout: 20_000, message: 'nothing left rendering' },
    )
    .toBe(0);

  // Back to the start, which the server has cached: the warm number.
  await page.keyboard.press('Home');
  const w1 = Date.now();
  await page.keyboard.press('Space');
  await showing(page, 'chunk');
  console.log(`PLAYBACK start from a cached place: ${Date.now() - w1} ms`);

  // An edit during playback: the old chunks go, playback carries on and returns to the composite of the new revision.
  // From here a 404 for a chunk of the old revision is expected (a render that was in flight when the edit landed).
  settled = true;
  const before = (await timeline(app)).revision;
  const reasked = page.waitForRequest((r) => r.url().includes('/project/preview-range?'), {
    timeout: 15_000,
  });
  app.applyOps([
    { op: 'add_track', id: 'ti2', kind: 'video' },
    { op: 'add_text_clip', id: 'cap2', track: 'ti2', text: 'Edited', duration: 20, start: 20 },
  ]);
  await expect.poll(async () => (await timeline(app)).revision).toBeGreaterThan(before);
  await expect(frame(page)).not.toHaveAttribute('data-playback', 'paused'); // playback goes on
  await reasked; // the chunks of the old revision are dropped and asked for again
  await showing(page, 'chunk');
  const e0 = await seconds(page);
  await expect.poll(() => seconds(page)).toBeGreaterThan(e0 + 1);
  await page.keyboard.press('Space');
  const labels = (await previewJobs(app)).filter((j) => j.state === 'done').length;
  expect(labels).toBeGreaterThanOrEqual(4);

  expect(errors).toEqual([]);
});

/** Keep the server's one preview worker busy for a few seconds, so the next chunk waits in the queue. */
async function hold(app: App, heights: number[]) {
  for (const height of heights) {
    await fetch(`${app.baseURL}/api/v1/project/preview-range?path=gig.mp4`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ start: 30, end: 60, height }),
      signal: AbortSignal.timeout(10_000),
    });
  }
}

for (const draftWorks of [true, false]) {
  test(`playback: while a chunk renders the ${draftWorks ? 'main track plays as a draft, then hands over' : 'still stays with "Preparing preview" when the draft cannot play'}`, async ({
    page,
    app,
  }) => {
    test.setTimeout(120_000);
    await openEditor(page, app);
    const codecs = await page.evaluate(() =>
      document.createElement('video').canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"'),
    );
    test.skip(codecs === '', 'this browser build cannot decode H.264 and AAC');
    if (!draftWorks) await page.route('**/api/v1/media/asset/**', (route) => route.abort());

    const ruler = (await page.getByTestId('ruler').boundingBox())!;
    await page.mouse.click(ruler.x + ruler.width * 0.5, ruler.y + ruler.height / 2);
    await expect(page.getByTestId('preview-still')).toBeVisible();
    await hold(app, [240, 220, 200, 180, 160, 140]);
    const t0 = Date.now();
    await page.keyboard.press('Space');

    if (draftWorks) {
      const badge = page.getByTestId('preview-draft-badge');
      await expect(badge).toHaveText('Draft preview, overlays loading');
      await expect(frame(page)).toHaveAttribute('data-showing', 'draft');
      const draft = page.getByTestId('preview-draft');
      const a = await seconds(page);
      await expect.poll(() => seconds(page)).toBeGreaterThan(a); // the playhead follows the draft
      expect(await draft.evaluate((v: HTMLVideoElement) => !v.paused)).toBe(true);
      await shot(page, 'draft');
      console.log(`PLAYBACK draft picture after ${Date.now() - t0} ms`);
      await showing(page, 'chunk', HANDOVER_MS);
      console.log(`PLAYBACK composite after ${Date.now() - t0} ms behind the held worker`);
      // One sound at a time: the draft is paused and gone the moment the composite plays.
      expect(await draft.evaluate((v: HTMLVideoElement) => v.paused)).toBe(true);
      await expect(badge).toHaveCount(0);
    } else {
      await expect(page.getByTestId('preview-preparing')).toHaveText('Preparing preview');
      await expect(frame(page)).toHaveAttribute('data-showing', 'still');
      await shot(page, 'preparing');
      await showing(page, 'chunk', HANDOVER_MS);
      await expect(page.getByTestId('preview-preparing')).toHaveCount(0);
    }
    await page.keyboard.press('Space');
    await expect(frame(page)).toHaveAttribute('data-playback', 'paused');
  });
}
