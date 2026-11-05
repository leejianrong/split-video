import { test, expect, openEditor, timeline } from './fixtures';

// Undo and redo (SPL-75): Ctrl+Z / Ctrl+Shift+Z and the toolbar buttons step through the engine's history, which
// survives a reload. Each step is a new revision (undo is a forward write).

const VIEWPORTS = [
  { width: 1440, height: 900 },
  { width: 1280, height: 720 },
];

test("edit, undo, redo, reload: the history is the engine's and survives", async ({
  page,
  app,
}, testInfo) => {
  await openEditor(page, app);
  const undo = page.getByRole('button', { name: 'Undo' });
  const redo = page.getByRole('button', { name: 'Redo' });
  await expect(page.locator('[data-clip-id]').first()).toBeVisible({ timeout: 20_000 });
  await expect(undo).toBeDisabled();
  await expect(redo).toBeDisabled();
  const first = await timeline(app);
  const count = () => timeline(app).then((t) => t.tracks.flatMap((tr) => tr.clips).length);
  expect(await count()).toBe(1);

  const ruler = (await page.getByTestId('ruler').boundingBox())!;
  await page.mouse.click(ruler.x + ruler.width * 0.4, ruler.y + ruler.height / 2);
  await page.keyboard.press('s');
  await expect.poll(count).toBe(2);
  await expect(undo).toBeEnabled();
  await expect(redo).toBeDisabled();
  const edited = await timeline(app);
  expect(edited.revision).toBe(first.revision + 1);

  const shot = async (name: string) => {
    for (const size of VIEWPORTS) {
      await page.setViewportSize(size);
      const box = async (region: string) =>
        (await page.locator(`[data-region="${region}"]`).boundingBox())!;
      expect((await box('top-bar')).height).toBe(48);
      expect((await box('timeline')).height).toBe(292);
      await page.screenshot({
        path: `${process.env.E2E_SHOT_DIR ?? testInfo.outputPath('.')}/undo-${name}-${size.width}.png`,
      });
    }
  };
  await shot('edited');

  // Ctrl+Z undoes the split as a NEW revision whose content is the previous one.
  await page.keyboard.press('Control+z');
  await expect.poll(count).toBe(1);
  const undone = await timeline(app);
  expect(undone.revision).toBe(edited.revision + 1);
  await expect(undo).toBeDisabled();
  await expect(redo).toBeEnabled();
  await shot('undone');

  await page.keyboard.press('Control+Shift+z');
  await expect.poll(count).toBe(2);
  expect((await timeline(app)).revision).toBe(undone.revision + 1);
  await expect(redo).toBeDisabled();

  // The history survives a reload: undo works on the fresh page, from the button too.
  await page.reload();
  await expect(page.locator('[data-clip-id]').first()).toBeVisible({ timeout: 20_000 });
  await expect(undo).toBeEnabled();
  await undo.click();
  await expect.poll(count).toBe(1);
  await expect(redo).toBeEnabled();
  await shot('reloaded');
});

test('Ctrl+Z inside a text field is left to the field', async ({ page, app }) => {
  await openEditor(page, app);
  await expect(page.locator('[data-clip-id]').first()).toBeVisible({ timeout: 20_000 });
  const ruler = (await page.getByTestId('ruler').boundingBox())!;
  await page.mouse.click(ruler.x + ruler.width * 0.4, ruler.y + ruler.height / 2);
  await page.keyboard.press('s');
  await expect.poll(async () => (await timeline(app)).tracks[0]!.clips.length).toBe(2);
  const revision = (await timeline(app)).revision;
  await page.evaluate(() => {
    const input = document.createElement('input');
    input.id = 'scratch';
    document.body.append(input);
    input.focus();
  });
  await page.keyboard.press('Control+z');
  await page.waitForTimeout(500);
  expect((await timeline(app)).revision).toBe(revision);
});
