import { test, expect, openEditor, timeline } from './fixtures';

test('live refresh: an outside CLI edit shows up in the open editor without a reload', async ({
  page,
  app,
}) => {
  await openEditor(page, app);
  const first = (await timeline(app)).tracks[0]!.clips[0]!;
  const marker = await page.evaluate(() => {
    (window as unknown as { __kept: number }).__kept = 1;
    return 1;
  });
  expect(marker).toBe(1);

  // An agent trims the clip and adds a second one through the same engine the browser uses.
  const out = app.applyOps([
    { op: 'trim_clip', clip: first.id, in: 0, out: 20 },
    { op: 'add_clip', id: 'agent1', asset: first.asset, in: 20, out: 40 },
  ]);
  expect(out).toContain('applied 2 ops');

  await expect(page.locator('[data-clip-id="agent1"]').first()).toBeVisible({ timeout: 20_000 });
  await expect(page.locator(`[data-clip-id="${first.id}"]`).first()).toHaveAttribute(
    'data-end',
    '20',
  );
  // The page was not reloaded: the marker set before the edit is still there.
  expect(await page.evaluate(() => (window as unknown as { __kept?: number }).__kept)).toBe(1);
});

test('live refresh: removing the selected clip from outside clears the selection', async ({
  page,
  app,
}) => {
  await openEditor(page, app);
  const first = (await timeline(app)).tracks[0]!.clips[0]!;
  app.applyOps([{ op: 'add_clip', id: 'agent2', asset: first.asset, in: 0, out: 10 }]);
  const added = page.locator('[data-clip-id="agent2"]').first();
  await expect(added).toBeVisible({ timeout: 20_000 });
  await added.click();
  await expect(page.locator('[data-testid=clip][aria-selected=true]').first()).toBeVisible();

  app.applyOps([{ op: 'remove_clip', clip: 'agent2' }]);
  await expect(page.locator('[data-clip-id="agent2"]')).toHaveCount(0, { timeout: 20_000 });
  await expect(page.locator('[data-testid=clip][aria-selected=true]')).toHaveCount(0);
});
