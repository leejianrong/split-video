import { test, expect, openEditor, timeline } from './fixtures';
import { EXTRA_PATH } from './global-setup';

test('media tab: import a second recording and drag it to the timeline', async ({ page, app }) => {
  await openEditor(page, app);
  const before = (await timeline(app)).tracks.flatMap((t) => t.clips).length;

  await page.setInputFiles('[data-testid=import-input]', EXTRA_PATH);
  const cards = page.getByTestId('media-card');
  await expect(cards).toHaveCount(2);
  const card = cards.filter({ hasText: 'extra.mp4' });
  await expect(card).toBeVisible();

  // The card is draggable with the asset payload the timeline accepts (state/dnd.ts); drop it on the lanes.
  await card.locator('button.select').dragTo(page.getByTestId('lanes'));

  await expect
    .poll(async () => (await timeline(app)).tracks.flatMap((t) => t.clips).length, {
      timeout: 15_000,
    })
    .toBeGreaterThan(before);
  const clips = (await timeline(app)).tracks.flatMap((t) => t.clips);
  const added = clips.filter((c) => c.out_point - c.in_point === 8);
  expect(added.length).toBeGreaterThan(0);
  await expect(page.locator(`[data-clip-id="${added[0]!.id}"]`).first()).toBeVisible();
});

test('media tab: the add button puts a recording on the timeline', async ({ page, app }) => {
  await openEditor(page, app);
  await page.setInputFiles('[data-testid=import-input]', EXTRA_PATH);
  const card = page.getByTestId('media-card').filter({ hasText: 'extra.mp4' });
  await expect(card).toBeVisible();
  const before = (await timeline(app)).tracks.flatMap((t) => t.clips).length;
  await card.getByRole('button', { name: /^Add extra\.mp4 to the timeline/ }).click();
  await expect
    .poll(async () => (await timeline(app)).tracks.flatMap((t) => t.clips).length)
    .toBeGreaterThan(before);
});
