import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PreviewStore } from './preview.svelte';
import { STILL_DELAY_MS } from './stills';

const still = (time: number, ratio = 16 / 9) => ({ blob: new Blob(['x']), time, ratio });
const key = (time: number, path = 'gig.mp4') => ({ path, time, width: 640, revision: 1 });

describe('PreviewStore', () => {
  let n = 0;
  const created = vi.fn(() => `blob:still-${++n}`);
  const revoked = vi.fn();

  beforeEach(() => {
    vi.useFakeTimers();
    n = 0;
    created.mockClear();
    revoked.mockClear();
    URL.createObjectURL = created;
    URL.revokeObjectURL = revoked;
  });
  afterEach(() => vi.useRealTimers());

  it('keeps the previous picture while the next renders, and revokes it once replaced', async () => {
    const fetcher = vi.fn(async (k: { time: number }) => still(k.time));
    const store = new PreviewStore(fetcher);
    store.request(key(1));
    await vi.advanceTimersByTimeAsync(STILL_DELAY_MS);
    expect(store.url).toBe('blob:still-1');
    expect(store.busy).toBe(false);

    store.request(key(2));
    expect(store.busy).toBe(true);
    expect(store.url).toBe('blob:still-1');
    await vi.advanceTimersByTimeAsync(STILL_DELAY_MS);
    expect(store.url).toBe('blob:still-2');
    expect(revoked).toHaveBeenCalledWith('blob:still-1');
  });

  it('takes the ratio from the server, keeps the picture on an error, and clears on demand', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(still(1, 9 / 16))
      .mockRejectedValueOnce(new Error('no'));
    const store = new PreviewStore(fetcher);
    store.request(key(1));
    await vi.advanceTimersByTimeAsync(STILL_DELAY_MS);
    expect(store.ratio).toBeCloseTo(9 / 16);

    store.request(key(2));
    await vi.advanceTimersByTimeAsync(STILL_DELAY_MS);
    expect(store.error?.message).toBe('no');
    expect(store.url).toBe('blob:still-1');

    store.clear();
    expect(store.url).toBeNull();
    expect(store.error).toBeNull();
    expect(revoked).toHaveBeenCalledWith('blob:still-1');
  });

  it('drops the old picture when another project is requested', async () => {
    const store = new PreviewStore(async (k) => still(k.time));
    store.request(key(1));
    await vi.advanceTimersByTimeAsync(STILL_DELAY_MS);
    store.request(key(1, 'other.mp4'));
    expect(store.url).toBeNull();
  });
});
