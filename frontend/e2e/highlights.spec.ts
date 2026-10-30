import { test, expect, openEditor, runQuickAction, timeline } from './fixtures';

test('highlights: find, review the cards and make a short', async ({ page, app }) => {
  await openEditor(page, app);

  const run = await runQuickAction(page, 'highlights');
  await expect(run).toContainText('highlights found');
  await page.getByTestId('review-highlights').click();

  const cards = page.getByTestId('highlight-card');
  await expect(cards.first()).toBeVisible();
  expect(await cards.count()).toBeGreaterThanOrEqual(1);
  await expect(cards.first().getByTestId('highlight-range')).toContainText('–');

  const before = (await timeline(app)).tracks.flatMap((t) => t.clips).length;
  await cards.first().getByTestId('make-short').click();

  // The short is a labelled, cropped (9:16) clip appended to the main track, and the card reads Accepted.
  await expect
    .poll(async () => (await timeline(app)).tracks.flatMap((t) => t.clips).length, {
      timeout: 15_000,
    })
    .toBe(before + 1);
  const clips = (await timeline(app)).tracks.flatMap((t) => t.clips);
  const short = clips.find((c) => c.label.startsWith('Short:'));
  expect(short, 'a clip labelled "Short: ..."').toBeDefined();
  expect(short!.crop).not.toBeNull();
  await expect(cards.first().getByTestId('accepted')).toBeVisible();
  await expect(page.locator(`[data-clip-id="${short!.id}"]`).first()).toBeVisible();
});

test('highlights: dismissing a card removes it from the list', async ({ page, app }) => {
  await openEditor(page, app);
  await runQuickAction(page, 'highlights');
  await page.getByTestId('review-highlights').click();
  const cards = page.getByTestId('highlight-card');
  await expect(cards.first()).toBeVisible();
  const count = await cards.count();
  await cards.first().getByTestId('dismiss').click();
  await expect(cards).toHaveCount(count - 1);
});
