import { test, expect, openEditor, timeline } from './fixtures';

// Previous/next cut and J/K/L transport (SPL-127): Up/Down jump to the nearest clip edge on any track, J/L step 5 s,
// Comma/Period one frame; a jump while playing keeps playing. The keys are listed in the `?` overlay.

const VIEWPORTS = [
  { width: 1440, height: 900 },
  { width: 1280, height: 720 },
];
const FRAME = 1 / 30;

test('Up/Down land on clip edges of any track, J/L and Comma/Period step, playing carries on', async ({
  page,
  app,
}, testInfo) => {
  test.setTimeout(150_000);
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  // A fresh recording's waveform is a known 404 until it is computed; any other failed request is an error.
  page.on('response', (r) => {
    if (r.status() >= 400 && !/waveform/.test(r.url())) errors.push(`${r.status()} ${r.url()}`);
  });
  await openEditor(page, app);
  const main = (await timeline(app)).tracks[0]!.clips[0]!;
  app.applyOps([
    { op: 'add_track', id: 'ov', kind: 'video' },
    { op: 'add_clip', id: 'pip', track: 'ov', asset: main.asset, in: 0, out: 20, start: 25 },
  ]);
  await expect(page.locator('[data-clip-id="pip"]').first()).toBeVisible({ timeout: 20_000 });
  const t = await timeline(app);
  const points = [
    ...new Set([
      0,
      t.duration,
      ...t.tracks.flatMap((x) => x.clips.flatMap((c) => [c.start, c.end])),
    ]),
  ].sort((a, b) => a - b);
  expect(points).toEqual([0, 25, 45, t.duration]);

  const head = page.getByTestId('playhead');
  const now = async () => Number(await head.getAttribute('data-time'));
  const shot = async (name: string) => {
    for (const size of VIEWPORTS) {
      await page.setViewportSize(size);
      const box = async (region: string) =>
        (await page.locator(`[data-region="${region}"]`).boundingBox())!;
      expect((await box('top-bar')).height).toBe(48);
      expect((await box('left')).width).toBe(308 + 72);
      expect((await box('inspector')).width).toBe(340);
      expect((await box('timeline')).height).toBe(292);
      await page.screenshot({
        path: `${process.env.E2E_SHOT_DIR ?? testInfo.outputPath('.')}/transport-${name}-${size.width}.png`,
      });
    }
    await page.setViewportSize(VIEWPORTS[0]!);
  };

  // Cuts: the edge of the overlay clip counts although the main track has none there, on both the button and the key.
  await page.keyboard.press('Home');
  await page.keyboard.press('ArrowDown');
  await expect.poll(now).toBe(25);
  await page.getByRole('button', { name: 'Next cut' }).click();
  await expect.poll(now).toBe(45);
  await page.keyboard.press('ArrowUp');
  await expect.poll(now).toBe(25);
  await page.getByRole('button', { name: 'Previous cut' }).click();
  await expect.poll(now).toBe(0);
  await page.keyboard.press('ArrowUp');
  expect(await now()).toBe(0);

  // Steps: J/L are 5 s, Comma/Period one frame at 30 fps.
  await page.keyboard.press('l');
  await expect.poll(now).toBe(5);
  await page.keyboard.press('Period');
  await expect.poll(now).toBeCloseTo(5 + FRAME, 2);
  await page.keyboard.press('Comma');
  await page.keyboard.press('Comma');
  await expect.poll(now).toBeCloseTo(5 - FRAME, 2);
  await page.keyboard.press('j');
  await expect.poll(now).toBe(0);
  await expect(page.getByTestId('preview-still')).toBeVisible();
  await shot('paused');

  // The keys are in the shortcut help.
  await page.keyboard.press('?');
  const help = page.getByRole('dialog', { name: 'Keyboard shortcuts' });
  await expect(help).toContainText('Step back / forward 5 s');
  await expect(help).toContainText('Previous / next frame');
  await expect(help).toContainText('Playhead to previous / next cut');
  await shot('help');
  await page.keyboard.press('Escape');
  await expect(help).toBeHidden();

  // Playing: a jump seeks and playback goes on from there.
  const codecs = await page.evaluate(() =>
    document.createElement('video').canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"'),
  );
  test.skip(codecs === '', 'this browser build cannot decode H.264 and AAC');
  await page.getByTestId('play').click();
  const frame = page.getByTestId('preview-frame');
  await expect(frame).toHaveAttribute('data-playback', 'playing', { timeout: 60_000 });
  await page.keyboard.press('ArrowDown');
  await expect.poll(now, { timeout: 5_000 }).toBeGreaterThanOrEqual(25);
  await expect(frame).toHaveAttribute('data-playback', 'playing', { timeout: 60_000 });
  const landed = await now();
  await expect.poll(now, { timeout: 20_000 }).toBeGreaterThan(landed + 0.5);
  await shot('playing');
  await page.keyboard.press('k');
  await expect(frame).toHaveAttribute('data-playback', 'paused');
  expect(errors).toEqual([]);
});
