import { test, expect, openApp, timeline } from './fixtures';
import { GIG_PATH } from './global-setup';
import { SONGS } from './media';

// An empty workspace: the page the first launch shows.
test.use({ seedMedia: false });

test('first run: import a recording, split it into songs and open the editor', async ({
  page,
  app,
}) => {
  await openApp(page, app);
  await expect(page.getByTestId('drop-zone')).toBeVisible();

  await page.setInputFiles('[data-testid=file-input]', GIG_PATH);
  await expect(page.getByTestId('chosen-name')).toHaveText('gig.mp4');
  await expect(page.getByTestId('chosen-length')).toBeVisible();

  await page.getByTestId('goal-songs').click();
  const analysis = page.getByTestId('step-analysis');
  await expect(analysis).toHaveAttribute('data-state', 'done', { timeout: 60_000 });
  await expect(analysis).toContainText(`${SONGS} songs found`);
  await expect(page.getByTestId('step-preview')).toHaveAttribute('data-state', 'done', {
    timeout: 60_000,
  });

  await page.getByTestId('open-editor').click();
  await expect(page.locator('[data-region=top-bar]')).toBeVisible();
  // The goal opens the Media tab, and the timeline holds one labelled segment per song.
  await expect(page.locator('[role=tab][aria-selected=true]').first()).toHaveText(/Media/);
  const main = (await timeline(app)).tracks[0]!;
  expect(main.clips.map((c) => c.label)).toEqual(['Song 1', 'Song 2', 'Song 3']);
  await expect(page.getByTestId('clip').first()).toBeVisible();
});

test('first run: a bad file reports a readable error with a fix', async ({ page, app }) => {
  await openApp(page, app);
  await page.setInputFiles('[data-testid=file-input]', {
    name: 'notes.mp4',
    mimeType: 'video/mp4',
    buffer: Buffer.from('this is not a video'),
  });
  const error = page.getByTestId('upload-error');
  await expect(error).toBeVisible({ timeout: 30_000 });
  await expect(error).toContainText('Fix:');
});
