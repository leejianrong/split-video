import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { test, expect, openEditor, timeline } from './fixtures';

test('export: render the timeline to a tiny file from the dialog', async ({ page, app }) => {
  await openEditor(page, app);
  // Keep the render small: shorten the recording to 6 s from outside, the way an agent would.
  const clip = (await timeline(app)).tracks[0]!.clips[0]!;
  app.applyOps([{ op: 'trim_clip', clip: clip.id, in: 0, out: 6 }]);
  await expect(page.locator(`[data-clip-id="${clip.id}"]`).first()).toHaveAttribute(
    'data-end',
    '6',
  );

  await page.getByTestId('export-button').click();
  const dialog = page.locator('dialog[open]');
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('The whole timeline as one video').check();
  await page.selectOption('[data-testid=export-preset]', 'draft-720');
  await page.fill('[data-testid=export-folder]', 'out');
  await page.getByTestId('start-export').click();

  const row = page.getByTestId('queue-row').first();
  await expect(row).toHaveAttribute('data-state', 'done', { timeout: 90_000 });
  await expect(row).toContainText('Saved in out');

  const files = fs.readdirSync(path.join(app.dir, 'out'));
  expect(files).toEqual(['gig.mp4']);
  const probe = execFileSync(
    'ffprobe',
    [
      '-v',
      'error',
      '-show_entries',
      'format=duration:stream=height',
      '-of',
      'json',
      path.join(app.dir, 'out', files[0]!),
    ],
    { timeout: 30_000 },
  ).toString();
  const info = JSON.parse(probe) as {
    format: { duration: string };
    streams: { height?: number }[];
  };
  expect(Number(info.format.duration)).toBeGreaterThan(5);
  expect(Number(info.format.duration)).toBeLessThan(7.5);
  expect(info.streams.some((s) => s.height === 720)).toBe(true);
});

test('export: a file that already exists fails with a fix and does not stop the app', async ({
  page,
  app,
}) => {
  await openEditor(page, app);
  const clip = (await timeline(app)).tracks[0]!.clips[0]!;
  app.applyOps([{ op: 'trim_clip', clip: clip.id, in: 0, out: 4 }]);
  fs.mkdirSync(path.join(app.dir, 'taken'));
  fs.writeFileSync(path.join(app.dir, 'taken', 'gig.mp4'), 'already here');

  await page.getByTestId('export-button').click();
  const dialog = page.locator('dialog[open]');
  await dialog.getByLabel('The whole timeline as one video').check();
  await page.selectOption('[data-testid=export-preset]', 'draft-720');
  await page.fill('[data-testid=export-folder]', 'taken');
  await page.getByTestId('start-export').click();

  const row = page.getByTestId('queue-row').first();
  await expect(row).toHaveAttribute('data-state', 'failed', { timeout: 60_000 });
  await expect(page.getByTestId('row-error')).toContainText('Fix');
  expect(fs.readFileSync(path.join(app.dir, 'taken', 'gig.mp4'), 'utf8')).toBe('already here');
});
