import { test, expect, openApp, openEditor } from './fixtures';

// A state the editor expects (a waveform still being made) is not an error: no 4xx response and no console error
// while the project loads and the timeline prepares its audio lane (SPL-139).
test('opening a project logs no 4xx response and no console error', async ({ page, app }) => {
  const bad: string[] = [];
  page.on('response', (r) => {
    if (r.status() >= 400) bad.push(`${r.status()} ${r.url()}`);
  });
  page.on('console', (m) => {
    if (m.type() === 'error') bad.push(`console: ${m.text()}`);
  });
  await openApp(page, app);
  await openEditor(page, app);
  await expect(page.getByTestId('clip').first()).toBeVisible();
  await expect(page.getByText('Preparing the waveform...')).toHaveCount(0, { timeout: 60_000 });
  expect(bad).toEqual([]);
});
