import type { Page } from '@playwright/test';
import { test, expect, openEditor, timeline, type App, type TimelineJson } from './fixtures';

// Split at the playhead (SPL-76): S cuts the selected clip, or the topmost one, as one edit.

/** One clip per track kind, all covering 30..60 s, so a playhead at the middle of the gig is inside every one. */
async function scene(app: App) {
  const main = (await timeline(app)).tracks[0]!.clips[0]!;
  app.applyOps([
    { op: 'add_track', id: 'tl', kind: 'video' },
    { op: 'add_track', id: 'ov', kind: 'video' },
    { op: 'add_track', id: 'au', kind: 'audio' },
    { op: 'add_text_clip', id: 'title', track: 'tl', text: 'Hello', duration: 30, start: 30 },
    { op: 'add_clip', id: 'pip', track: 'ov', asset: main.asset, in: 0, out: 30, start: 30 },
    { op: 'add_clip', id: 'song', track: 'au', asset: main.asset, in: 0, out: 30, start: 30 },
  ]);
  return main.id;
}

async function playheadAt(page: Page, fraction: number) {
  const box = (await page.getByTestId('ruler').boundingBox())!;
  await page.mouse.click(box.x + box.width * fraction, box.y + box.height / 2);
}

const clips = (t: TimelineJson) => t.tracks.flatMap((track) => track.clips);
const trackOf = (t: TimelineJson, id: string) =>
  t.tracks.find((track) => track.clips.some((c) => c.id === id))!;

test('S splits each track kind at the playhead: continuous in/out, same total length', async ({
  page,
  app,
}) => {
  const mainId = await scene(app);
  await openEditor(page, app);
  await expect(page.locator('[data-clip-id="song"]').first()).toBeVisible({ timeout: 20_000 });
  const before = await timeline(app);
  await playheadAt(page, 0.4);

  let revision = before.revision;
  for (const id of [mainId, 'title', 'pip', 'song']) {
    await page.locator(`[data-clip-id="${id}"]`).first().click();
    await page.keyboard.press('s');
    // One split is one edit: exactly one new revision.
    await expect.poll(async () => (await timeline(app)).revision).toBe(revision + 1);
    revision += 1;
    // The selection follows to the right-hand half.
    const cut = trackOf(await timeline(app), id).clips;
    const right = cut[cut.findIndex((c) => c.id === id) + 1]!;
    await expect(
      page.locator(`[data-clip-id="${right.id}"][aria-selected=true]`).first(),
    ).toBeVisible();
  }

  const after = await timeline(app);
  expect(clips(after)).toHaveLength(clips(before).length + 4);
  expect(after.duration).toBeCloseTo(before.duration, 6);
  for (const id of [mainId, 'title', 'pip', 'song']) {
    const cut = trackOf(after, id).clips;
    const i = cut.findIndex((c) => c.id === id);
    const [left, right] = [cut[i]!, cut[i + 1]!];
    expect(left.end).toBeCloseTo(right.start, 6);
    expect(left.out_point).toBeCloseTo(right.in_point, 6);
    expect(left.end - left.start).toBeGreaterThanOrEqual(0.1);
    expect(right.end - right.start).toBeGreaterThanOrEqual(0.1);
  }
});

test('S with nothing selected cuts the topmost clip; off a clip it says so and changes nothing', async ({
  page,
  app,
}) => {
  await scene(app);
  await openEditor(page, app);
  await expect(page.locator('[data-clip-id="song"]').first()).toBeVisible({ timeout: 20_000 });
  const before = await timeline(app);
  await playheadAt(page, 0.4);
  await page.keyboard.press('s');
  await expect.poll(async () => (await timeline(app)).revision).toBe(before.revision + 1);
  const after = await timeline(app);
  // The overlay lane is the latest track, so it is the topmost lane; the others are untouched.
  expect(trackOf(after, 'pip').clips).toHaveLength(2);
  expect(trackOf(after, 'title').clips).toHaveLength(1);
  expect(trackOf(after, 'song').clips).toHaveLength(1);
  expect(clips(after)).toHaveLength(clips(before).length + 1);

  await page.keyboard.press('Escape');
  await playheadAt(page, 0); // the start of the recording: the edge of the main clip, off every other clip
  await page.keyboard.press('s');
  await expect(
    page.locator('[data-kind=warning]').filter({ hasText: 'not on a clip' }),
  ).toBeVisible();
  expect((await timeline(app)).revision).toBe(after.revision);
});
