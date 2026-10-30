import { test, expect, openEditor, timeline } from './fixtures';

test('inspector: change a clip volume and see it saved, then survive a reload', async ({
  page,
  app,
}) => {
  await openEditor(page, app);
  const clip = page.getByTestId('clip').first();
  await clip.click();
  await expect(page.getByTestId('clip-inspector')).toBeVisible();

  await page.locator('[data-region=inspector] [role=tab]', { hasText: 'Audio' }).click();
  const volume = page.getByLabel('Volume (dB)');
  await volume.fill('-6');
  await volume.press('Enter');

  // -6 dB is a gain of about 0.5; the engine stored it, so the server's timeline has it.
  await expect
    .poll(async () => (await timeline(app)).tracks[0]!.clips[0]!.gain, { timeout: 15_000 })
    .toBeCloseTo(0.501, 2);

  await page.reload();
  await page.getByTestId('clip').first().click();
  await page.locator('[data-region=inspector] [role=tab]', { hasText: 'Audio' }).click();
  await expect(page.getByLabel('Volume (dB)')).toHaveValue('-6');
});

test('inspector: an out of range value is refused with a message and nothing is saved', async ({
  page,
  app,
}) => {
  await openEditor(page, app);
  await page.getByTestId('clip').first().click();
  await page.locator('[data-region=inspector] [role=tab]', { hasText: 'Audio' }).click();
  const before = (await timeline(app)).revision;
  const volume = page.getByLabel('Volume (dB)');
  await volume.fill('500');
  await volume.press('Enter');
  await expect(page.locator('[data-region=inspector]')).toContainText(
    'Volume must be from -40 to 12 dB. Fix:',
  );
  expect((await timeline(app)).revision).toBe(before);
});
