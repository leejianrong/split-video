import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { STILL_DELAY_MS, StillScheduler, type StillKey } from './stills';

const key = (time: number, over: Partial<StillKey> = {}): StillKey => ({
  path: 'gig.mp4',
  time,
  width: 640,
  revision: 1,
  ...over,
});

interface Pending {
  key: StillKey;
  signal: AbortSignal;
  resolve(value: string): void;
  reject(cause: unknown): void;
}

function setup() {
  const pending: Pending[] = [];
  const stills: string[] = [];
  const errors: unknown[] = [];
  const busy: boolean[] = [];
  const scheduler = new StillScheduler<string>(
    (k, signal) =>
      new Promise<string>((resolve, reject) => pending.push({ key: k, signal, resolve, reject })),
    {
      onStill: (s) => stills.push(s),
      onError: (e) => errors.push(e),
      onBusy: (b) => busy.push(b),
    },
  );
  return { scheduler, pending, stills, errors, busy };
}

describe('StillScheduler', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('sends one request after the playhead settles, not one per move', () => {
    const { scheduler, pending } = setup();
    for (let t = 0; t < 10; t++) {
      scheduler.request(key(t));
      vi.advanceTimersByTime(STILL_DELAY_MS - 20);
    }
    expect(pending).toHaveLength(0);
    vi.advanceTimersByTime(STILL_DELAY_MS);
    expect(pending).toHaveLength(1);
    expect(pending[0]?.key.time).toBe(9);
  });

  it('aborts the request in flight when a newer one starts, and ignores its late answer', async () => {
    const { scheduler, pending, stills } = setup();
    scheduler.request(key(1));
    vi.advanceTimersByTime(STILL_DELAY_MS);
    scheduler.request(key(2));
    expect(pending[0]?.signal.aborted).toBe(true);
    vi.advanceTimersByTime(STILL_DELAY_MS);
    expect(pending).toHaveLength(2);

    pending[1]?.resolve('two');
    await vi.advanceTimersByTimeAsync(0);
    pending[0]?.resolve('one'); // arrives after the newer one, from a fetch that ignored the abort
    await vi.advanceTimersByTimeAsync(0);
    expect(stills).toEqual(['two']);
  });

  it('drops an answer that arrives after a newer request was made, even before it fires', async () => {
    const { scheduler, pending, stills } = setup();
    scheduler.request(key(1));
    vi.advanceTimersByTime(STILL_DELAY_MS);
    scheduler.request(key(2)); // the timer for 2 has not fired yet
    pending[0]?.resolve('one');
    await vi.advanceTimersByTimeAsync(0);
    expect(stills).toEqual([]);
  });

  it('is busy from the request until the still arrives', async () => {
    const { scheduler, pending, busy } = setup();
    scheduler.request(key(1));
    vi.advanceTimersByTime(STILL_DELAY_MS);
    expect(busy).toEqual([true]);
    pending[0]?.resolve('one');
    await vi.advanceTimersByTimeAsync(0);
    expect(busy).toEqual([true, false]);
  });

  it('does not ask again for the key already shown, but does for a new revision or width', async () => {
    const { scheduler, pending } = setup();
    scheduler.request(key(1));
    vi.advanceTimersByTime(STILL_DELAY_MS);
    pending[0]?.resolve('one');
    await vi.advanceTimersByTimeAsync(0);
    scheduler.request(key(1));
    vi.advanceTimersByTime(STILL_DELAY_MS);
    expect(pending).toHaveLength(1);
    scheduler.request(key(1, { revision: 2 }));
    vi.advanceTimersByTime(STILL_DELAY_MS);
    scheduler.request(key(1, { revision: 2, width: 1280 }));
    vi.advanceTimersByTime(STILL_DELAY_MS);
    expect(pending).toHaveLength(3);
  });

  it('reports a failure once, stays quiet for an abort, and retries the same key afterwards', async () => {
    const { scheduler, pending, errors, busy } = setup();
    scheduler.request(key(1));
    vi.advanceTimersByTime(STILL_DELAY_MS);
    scheduler.request(key(2));
    pending[0]?.reject(new DOMException('aborted', 'AbortError'));
    await vi.advanceTimersByTimeAsync(0);
    expect(errors).toEqual([]);

    vi.advanceTimersByTime(STILL_DELAY_MS);
    pending[1]?.reject(new Error('boom'));
    await vi.advanceTimersByTimeAsync(0);
    expect(errors).toHaveLength(1);
    expect(busy.at(-1)).toBe(false);

    scheduler.request(key(2));
    vi.advanceTimersByTime(STILL_DELAY_MS);
    expect(pending).toHaveLength(3);
  });

  it('cancel() stops the timer and the request in flight', async () => {
    const { scheduler, pending, stills, busy } = setup();
    scheduler.request(key(1));
    scheduler.cancel();
    vi.advanceTimersByTime(STILL_DELAY_MS * 2);
    expect(pending).toHaveLength(0);
    scheduler.request(key(2));
    vi.advanceTimersByTime(STILL_DELAY_MS);
    scheduler.cancel();
    expect(pending[0]?.signal.aborted).toBe(true);
    pending[0]?.resolve('two');
    await vi.advanceTimersByTimeAsync(0);
    expect(stills).toEqual([]);
    expect(busy.at(-1)).toBe(false);
  });
});
