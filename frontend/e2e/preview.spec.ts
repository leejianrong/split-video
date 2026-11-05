import type { Page } from '@playwright/test';
import { test, expect, openEditor, timeline } from './fixtures';

/** A hash of the picture the preview shows now, read from its object URL. */
async function stillHash(page: Page): Promise<string> {
  return await page.getByTestId('preview-still').evaluate(async (img) => {
    const bytes = await (await fetch((img as HTMLImageElement).src)).arrayBuffer();
    const digest = await crypto.subtle.digest('SHA-1', bytes);
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
  });
}

test('preview: a still shows at the playhead, follows scrubbing, and redraws when an outside edit adds an overlay and a title', async ({
  page,
  app,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('response', (r) => {
    // (the waveform of a fresh recording is a known 404 until it is computed)
    if (r.status() >= 400 && r.url().includes('/preview-frame'))
      errors.push(`${r.status()} ${r.url()}`);
  });
  const requests: string[] = [];
  page.on('request', (r) => r.url().includes('/project/preview-frame') && requests.push(r.url()));

  await openEditor(page, app);
  const still = page.getByTestId('preview-still');
  await expect(still).toBeVisible();
  await expect(still).toHaveAttribute('data-time', '0');
  await expect
    .poll(() => still.evaluate((i) => (i as HTMLImageElement).naturalWidth))
    .toBeGreaterThan(100);

  // The frame follows the output's ratio (the picture's own) and sits inside the preview region.
  const frame = await page.getByTestId('preview-frame').boundingBox();
  const region = await page.locator('[data-region=preview]').boundingBox();
  const natural = await still.evaluate((i) => ({
    w: (i as HTMLImageElement).naturalWidth,
    h: (i as HTMLImageElement).naturalHeight,
  }));
  expect(frame!.width / frame!.height).toBeCloseTo(natural.w / natural.h, 1);
  expect(frame!.x).toBeGreaterThanOrEqual(region!.x);
  expect(frame!.x + frame!.width).toBeLessThanOrEqual(region!.x + region!.width + 0.5);
  expect(frame!.y + frame!.height).toBeLessThanOrEqual(region!.y + region!.height + 0.5);

  // Scrubbing: clicks on the ruler move the playhead; a burst makes one request for where it stopped.
  const ruler = (await page.getByTestId('ruler').boundingBox())!;
  const before = requests.length;
  for (const f of [0.3, 0.5, 0.7]) {
    await page.mouse.click(ruler.x + ruler.width * f, ruler.y + ruler.height / 2);
  }
  await expect(still).not.toHaveAttribute('data-time', '0');
  const shownAt = Number(await still.getAttribute('data-time'));
  expect(shownAt).toBeGreaterThan(20);
  const plain = await stillHash(page);
  expect(requests.length - before).toBeLessThanOrEqual(2);

  // An agent adds a picture-in-picture and a title over this moment: no reload, the still redraws.
  const first = (await timeline(app)).tracks[0]!.clips[0]!;
  const t = Math.floor(shownAt);
  app.applyOps([
    { op: 'add_track', id: 'ov', kind: 'video' },
    { op: 'add_clip', id: 'pip', asset: first.asset, track: 'ov', in: 0, out: 30, start: t - 5 },
    { op: 'set_clip_properties', clip: 'pip', scale: 0.3, x: 0.8, y: 0.2, muted: true },
    { op: 'add_track', id: 'ti', kind: 'video' },
    {
      op: 'add_text_clip',
      id: 'cap',
      track: 'ti',
      text: 'Wait for it',
      duration: 20,
      start: t - 5,
    },
  ]);
  // Same instant, different picture: the PiP and the title were drawn into it, and the shown time did not move.
  await expect.poll(() => stillHash(page), { timeout: 20_000 }).not.toBe(plain);
  await expect(still).toHaveAttribute('data-time', String(shownAt));

  // The regions keep the ux-spec sizes at 1440 x 900.
  const size = async (sel: string) => (await page.locator(sel).boundingBox())!;
  expect((await size('[data-region=top-bar]')).height).toBe(48);
  expect((await size('[data-region=timeline]')).height).toBe(292);
  expect(errors).toEqual([]);
});
