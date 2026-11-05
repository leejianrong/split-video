import { test, expect, timeline } from './fixtures';

const PATH = 'gig.mp4';

test('preview range: a chunk of the recording plays in a <video>, seeks, and is cached until an edit', async ({
  page,
  app,
}) => {
  const url = `${app.baseURL}/api/v1/project/preview-range`;
  const start = async () => {
    const r = await fetch(`${url}?path=${PATH}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ start: 5, end: 13 }),
      signal: AbortSignal.timeout(10_000),
    });
    expect(r.status).toBe(200);
    return (await r.json()) as { job_id: string; cached: boolean; key: string; revision: number };
  };

  await page.goto(`${app.baseURL}/app/`); // same origin as the API: the <video> may load the chunk
  const codecs = await page.evaluate(() =>
    document.createElement('video').canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"'),
  );
  test.skip(codecs === '', 'this browser build cannot decode H.264 and AAC');

  const first = await start();
  expect(first.cached).toBe(false);
  await expect
    .poll(
      async () => {
        const r = await fetch(`${app.baseURL}/api/v1/jobs/${first.job_id}`);
        return ((await r.json()) as { state: string }).state;
      },
      { timeout: 60_000, message: 'preview_range job state' },
    )
    .toBe('done');

  const played = await page.evaluate(
    async ([src]) => {
      const video = document.createElement('video');
      video.muted = true;
      video.preload = 'auto';
      document.body.append(video);
      const once = (name: string) =>
        new Promise<void>((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error(`no ${name} event within 20 s`)), 20_000);
          video.addEventListener(
            name,
            () => {
              clearTimeout(timer);
              resolve();
            },
            { once: true },
          );
          video.addEventListener(
            'error',
            () => reject(new Error(`media error ${video.error?.code}`)),
            {
              once: true,
            },
          );
        });
      const canplay = once('canplay');
      video.src = src!;
      await canplay;
      const seeked = once('seeked');
      video.currentTime = 4;
      await seeked;
      const atSeek = video.currentTime;
      await video.play();
      await new Promise((r) => setTimeout(r, 800));
      return {
        duration: video.duration,
        width: video.videoWidth,
        height: video.videoHeight,
        atSeek,
        after: video.currentTime,
        audioBytes: (video as unknown as { webkitAudioDecodedByteCount?: number })
          .webkitAudioDecodedByteCount,
      };
    },
    [`${url}/${first.key}?path=${PATH}`],
  );
  expect(played.duration).toBeCloseTo(8, 0);
  expect([played.width, played.height]).toEqual([320, 240]); // the recording's own size: never upscaled
  expect(played.atSeek).toBeCloseTo(4, 1);
  expect(played.after).toBeGreaterThan(4.3);
  expect(played.audioBytes ?? 1).toBeGreaterThan(0); // the sound was decoded, not only the picture

  // The same request again is a cache hit, and an edit makes the old chunk unavailable.
  const again = await start();
  expect(again).toMatchObject({ cached: true, key: first.key });
  const clip = (await timeline(app)).tracks[0]!.clips[0]!;
  app.applyOps([{ op: 'trim_clip', clip: clip.id, in: 1 }]);
  const after = await start();
  expect(after.cached).toBe(false);
  expect(after.revision).toBeGreaterThan(first.revision);
  const old = await fetch(`${url}/${first.key}?path=${PATH}`);
  expect(old.status).toBe(404);
});
