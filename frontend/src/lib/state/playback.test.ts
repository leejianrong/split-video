import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  CHUNK_SECONDS,
  LOOKAHEAD,
  PlaybackStore,
  type DraftSource,
  type VideoSlot,
} from './playback.svelte';
import type { ChunkApi } from './playbackApi';

class FakeSlot implements VideoSlot {
  src: string | null = null;
  time = 0;
  duration = 0;
  ended = false;
  playing = false;
  muted = false;
  playbackRate = 1;
  get source() {
    return this.src;
  }
  playError: Error | null = null;
  loadError: Error | null = null;
  constructor(readonly chunkLength: (src: string) => number) {}
  async load(src: string) {
    if (this.loadError) throw this.loadError;
    this.src = src;
    this.time = 0;
    this.duration = this.chunkLength(src);
    this.ended = false;
  }
  async seek(time: number) {
    this.time = time;
  }
  async play() {
    if (this.playError) throw this.playError;
    this.playing = true;
  }
  pause() {
    this.playing = false;
  }
  clear() {
    this.src = null;
    this.playing = false;
    this.duration = 0;
  }
}

interface Req {
  range: { start: number; end: number };
  preset: string;
  jobId: string;
  cached: boolean;
  signal: AbortSignal;
  finish(): void;
  fail(): void;
}

function setup(duration = 40, draft: DraftSource | null = null) {
  const reqs: Req[] = [];
  const cancels: string[] = []; // job ids, found by the request key the controller cancels with
  const jobOfKey = new Map<string, string>();
  let startGate: Promise<void> | null = null; // while set, a request has been sent but no job id has come back
  const fetched: string[] = [];
  const revoked: string[] = [];
  const cachedRanges = new Set<number>();
  let time = 0;
  const frames = new Set<() => void>();
  const lengths = new Map<string, number>();
  const api: ChunkApi = {
    async start(_path, range, requestKey, signal) {
      await startGate;
      const jobId = `job${reqs.length}`;
      jobOfKey.set(requestKey, jobId);
      const cached = cachedRanges.has(range.start);
      const finished = new Promise<void>((resolve, reject) => {
        reqs.push({
          range: { start: range.start, end: range.end },
          preset: range.preset,
          jobId,
          cached,
          signal,
          finish: () => resolve(),
          fail: () => reject(new Error('failed')),
        });
      });
      Object.assign(api, { [`wait_${jobId}`]: finished });
      return { jobId, cached, key: `k${range.start}` };
    },
    finished(jobId) {
      return (api as unknown as Record<string, Promise<void>>)[`wait_${jobId}`]!;
    },
    async cancel(requestKey) {
      cancels.push(jobOfKey.get(requestKey) ?? `unsent:${requestKey}`);
    },
    async fetch(_path, key) {
      fetched.push(key);
      const start = Number(key.slice(1));
      const blob = new Blob(['x']);
      lengths.set(`blob:${key}`, Math.min(start + CHUNK_SECONDS, duration) - start);
      return blob;
    },
  };
  const slots: [FakeSlot, FakeSlot] = [
    new FakeSlot((s) => lengths.get(s) ?? CHUNK_SECONDS),
    new FakeSlot((s) => lengths.get(s) ?? CHUNK_SECONDS),
  ];
  const draftSlot = new FakeSlot(() => 100);
  const store = new PlaybackStore({
    api,
    getTime: () => time,
    setTime: (t) => (time = t),
    everyFrame: (cb) => {
      frames.add(cb);
      return () => frames.delete(cb);
    },
    objectUrl: {
      create: (b) => {
        const key = fetched[fetched.length - 1]!;
        void b;
        return `blob:${key}`;
      },
      revoke: (u) => revoked.push(u),
    },
  });
  store.attach(slots, draftSlot, () => draft);
  store.sync({ path: 'gig.mp4', revision: 1, preset: 'youtube-1080p', duration });
  const flush = () => vi.advanceTimersByTimeAsync(0);
  const frame = () => frames.forEach((cb) => cb());
  /** Finish the render of the request at `i` (as the server does) and let the browser fetch the blob. */
  const finish = async (i: number) => {
    reqs[i]!.finish();
    await flush();
    await flush();
  };
  return {
    store,
    slots,
    draftSlot,
    reqs,
    cancels,
    holdStarts: () => {
      startGate = new Promise<void>(() => undefined);
    },
    revoked,
    cachedRanges,
    flush,
    frame,
    finish,
    setTime: (t: number) => (time = t),
    getTime: () => time,
    frames,
  };
}

describe('PlaybackStore', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('asks for the chunk of the playhead on the grid, then one render at a time up to the lookahead', async () => {
    const t = setup();
    t.setTime(11);
    t.store.play();
    await t.flush();
    expect(t.reqs.map((r) => r.range)).toEqual([{ start: 7, end: 16 }]); // from a second before the grid start (the lead-in)
    expect(t.store.state).toBe('buffering');
    await t.finish(0);
    expect(t.reqs.map((r) => r.range.start)).toEqual([7, 15]); // the next starts only after the first renders
    await t.finish(1);
    await t.finish(2);
    expect(t.reqs.map((r) => r.range)).toEqual([
      { start: 7, end: 16 },
      { start: 15, end: 24 },
      { start: 23, end: 32 },
    ]);
    expect(LOOKAHEAD).toBe(2);
    await t.finish(2);
    expect(t.reqs).toHaveLength(3); // nothing past current + 2
  });

  it('plays the ready chunk from the playhead offset and follows the video clock', async () => {
    const t = setup();
    t.setTime(11);
    t.store.play();
    await t.flush();
    await t.finish(0);
    expect(t.store.state).toBe('playing');
    expect(t.store.showing).toBe('chunk');
    const slot = t.slots[t.store.activeSlot!];
    expect(slot.playing).toBe(true);
    expect(slot.time).toBe(4); // 11 - 8, plus the 1 s lead-in in the file
    slot.time = 5.5;
    t.frame();
    expect(t.getTime()).toBe(12.5);
  });

  it('plays the next chunk muted from its lead-in, shows it when it catches up, and frees the old one', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    await t.finish(1);
    const first = t.store.activeSlot!;
    const other = first === 0 ? 1 : 0;
    expect(t.slots[other].src).toBe('blob:k7'); // loaded, parked in its lead-in
    expect(t.slots[other].playing).toBe(false);
    expect(t.slots[other].time).toBe(0.5);
    t.slots[first].time = 7.1;
    t.frame();
    expect(t.slots[other].playing).toBe(false); // too early
    t.slots[first].time = 7.3;
    t.frame();
    await t.flush();
    expect(t.slots[other].playing).toBe(true); // running under the old one, silent, hidden, slowed to catch up
    expect(t.slots[other].muted).toBe(true);
    expect(t.slots[other].playbackRate).toBeLessThan(1);
    expect(t.store.activeSlot).toBe(first);
    t.slots[first].time = 7.5;
    t.slots[other].time = 0.8; // 0.3 s ahead of the old one (timeline 7.8)
    t.frame();
    expect(t.store.activeSlot).toBe(first);
    t.slots[other].time = 0.6;
    t.slots[first].time = 7.6;
    t.frame(); // the old one has caught up (0.4 s before its end: the early swap): same picture, same instant
    expect(t.store.activeSlot).toBe(other);
    expect(t.slots[other].muted).toBe(false);
    expect(t.slots[other].playbackRate).toBe(1);
    expect(t.slots[first].playing).toBe(false);
    expect(t.getTime()).toBe(7.6);
    t.slots[other].time = 2.5;
    t.frame();
    expect(t.getTime()).toBe(9.5);
    expect(t.slots[first].src).toBe('blob:k0'); // released a moment later, not in the swap's frame
    await vi.advanceTimersByTimeAsync(400);
    expect(t.slots[first].src).toBeNull();
    await t.finish(2); // the chunk after next lands in the element that was freed
    expect(t.slots[first].src).toBe('blob:k15');
    expect(t.revoked).toContain('blob:k0');
    expect(t.reqs.map((r) => r.range.start)).toEqual([0, 7, 15, 23]);
  });

  it('swaps anyway near the old end when the next never lines up', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    await t.finish(1);
    const first = t.store.activeSlot!;
    t.slots[first].time = 7.3;
    t.frame();
    await t.flush();
    t.slots[first].time = 7.95;
    t.frame();
    expect(t.store.activeSlot).toBe(first === 0 ? 1 : 0);
  });

  it('holds and rebuffers when the next chunk is not ready at the join', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    t.slots[t.store.activeSlot!].time = 8;
    t.frame();
    expect(t.store.state).toBe('buffering');
    expect(t.store.showing).toBe('still');
    await t.finish(1);
    expect(t.store.state).toBe('playing');
    expect(t.slots[t.store.activeSlot!].src).toBe('blob:k7');
  });

  it('stops at the end of the timeline on a short last chunk', async () => {
    const t = setup(10);
    t.setTime(9);
    t.store.play();
    await t.flush();
    expect(t.reqs[0]!.range).toEqual({ start: 7, end: 10 });
    await t.finish(0);
    const slot = t.slots[t.store.activeSlot!];
    slot.time = 2;
    slot.ended = true;
    t.frame();
    expect(t.store.state).toBe('paused');
    expect(t.getTime()).toBe(10);
    expect(t.reqs).toHaveLength(1); // no chunk beyond the end
  });

  it('play at the very end starts over', async () => {
    const t = setup(10);
    t.setTime(10);
    t.store.play();
    await t.flush();
    expect(t.reqs[0]!.range).toEqual({ start: 0, end: 8 });
  });

  it('cancels by request key a render whose request was aborted before the job id came back', async () => {
    const t = setup();
    t.holdStarts();
    t.store.play();
    await t.flush();
    expect(t.reqs).toHaveLength(0);
    t.store.pause();
    await t.flush();
    expect(t.cancels).toHaveLength(1);
    expect(t.cancels[0]).toMatch(/^unsent:/);
  });

  it('cancels the render in flight when paused, and keeps the loaded chunk for a quick resume', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    await t.flush();
    expect(t.reqs).toHaveLength(2); // the lookahead render is running
    t.slots[t.store.activeSlot!].time = 2;
    t.frame();
    t.store.pause();
    expect(t.store.state).toBe('paused');
    expect(t.cancels).toEqual(['job1']);
    expect(t.reqs[1]!.signal.aborted).toBe(true);
    expect(t.getTime()).toBe(2);
    t.store.play();
    await t.flush();
    expect(t.store.state).toBe('playing');
    expect(t.reqs[2]!.range.start).toBe(7); // the lookahead resumes; chunk 0 was still in memory
  });

  it('a seek away cancels the old renders, asks for the new place first, and shows the still meanwhile', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    await t.flush();
    expect(t.reqs[1]!.range.start).toBe(7);
    t.setTime(26);
    t.store.seeked(26);
    expect(t.store.showing).toBe('still');
    expect(t.store.state).toBe('buffering');
    expect(t.cancels).toContain('job1');
    await t.flush();
    expect(t.reqs[2]!.range).toEqual({ start: 23, end: 32 }); // before any lookahead
    await t.finish(2);
    expect(t.store.state).toBe('playing');
    expect(t.slots[t.store.activeSlot!].time).toBe(3); // 26 - 24 + 1 s lead-in
    expect(t.reqs[3]!.range.start).toBe(31);
  });

  it('steps and cut jumps while playing seek and keep playing; paused they only move the playhead', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    await t.finish(1);
    t.setTime(3);
    t.store.step(5);
    expect(t.getTime()).toBe(8);
    t.store.cut([0, 12, 40], 'next');
    expect(t.getTime()).toBe(12);
    expect(t.store.state).not.toBe('paused');
    t.store.step(-100);
    expect(t.getTime()).toBe(0);
    t.store.step(1000);
    expect(t.getTime()).toBe(40);
    t.store.cut([0, 12, 40], 'next');
    expect(t.getTime()).toBe(40);
    t.store.pause();
    t.store.cut([0, 12, 40], 'prev');
    expect(t.getTime()).toBe(12);
    expect(t.store.state).toBe('paused');
  });

  it('a seek inside what is in memory is a seek in the video, with no request', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    await t.finish(1);
    const before = t.reqs.length;
    const nextSlot = t.store.activeSlot === 0 ? 1 : 0;
    t.setTime(10);
    t.store.seeked(10);
    await t.flush();
    expect(t.store.activeSlot).toBe(nextSlot);
    expect(t.slots[nextSlot].time).toBe(3); // 10 - 8, plus the lead-in
    expect(t.slots[nextSlot].playing).toBe(true);
    expect(t.reqs.length).toBe(before);
  });

  it('ignores the playhead moves it made itself', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    const slot = t.slots[t.store.activeSlot!];
    slot.time = 3;
    t.frame();
    t.store.seeked(3);
    expect(t.store.state).toBe('playing');
    expect(slot.playing).toBe(true);
  });

  it('an edit drops every chunk and rebuffers at the same time; playback carries on', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    await t.finish(1);
    const slot = t.slots[t.store.activeSlot!];
    slot.time = 5;
    t.frame();
    t.store.sync({ path: 'gig.mp4', revision: 2, preset: 'youtube-1080p', duration: 40 });
    expect(slot.playing).toBe(false); // the old sound stops at once
    expect(t.store.state).toBe('buffering');
    expect(t.revoked).toEqual(expect.arrayContaining(['blob:k0', 'blob:k7']));
    await t.flush();
    const again = t.reqs[t.reqs.length - 1]!;
    expect(again.range.start).toBe(0);
    await t.finish(t.reqs.length - 1);
    expect(t.store.state).toBe('playing');
    expect(t.slots[t.store.activeSlot!].time).toBe(5);
  });

  it('a canvas change drops the chunks like an edit and asks for the new preset', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    t.store.sync({ path: 'gig.mp4', revision: 1, preset: 'vertical-1080', duration: 40 });
    expect(t.revoked).toContain('blob:k0');
    await t.flush();
    expect(t.reqs[t.reqs.length - 1]!.preset).toBe('vertical-1080');
  });

  it('an edit while paused only drops the video for the still', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    t.store.pause();
    t.store.sync({ path: 'gig.mp4', revision: 2, preset: 'youtube-1080p', duration: 40 });
    expect(t.store.state).toBe('paused');
    expect(t.store.showing).toBe('still');
    expect(t.store.activeSlot).toBeNull();
  });

  it('plays the main track draft while the chunk renders, then hands over at the same time with one sound', async () => {
    const draft: DraftSource = {
      url: '/media/a',
      at: 7,
      until: 30,
      toTimeline: (s) => s - 4, // the clip starts at timeline 0 and is trimmed in at 4
    };
    const t = setup(40, draft);
    t.setTime(3);
    t.store.play();
    await t.flush();
    expect(t.store.showing).toBe('draft');
    expect(t.draftSlot.playing).toBe(true);
    expect(t.draftSlot.time).toBe(7);
    t.draftSlot.time = 8.5;
    t.frame();
    expect(t.getTime()).toBe(4.5);
    await t.finish(0);
    expect(t.store.showing).toBe('chunk');
    expect(t.draftSlot.playing).toBe(false);
    expect(t.draftSlot.src).toBeNull();
    expect(t.slots[t.store.activeSlot!].time).toBe(4.5);
    expect(t.slots[t.store.activeSlot!].playing).toBe(true);
  });

  it('keeps the still with "preparing" when the draft cannot be played', async () => {
    const draft: DraftSource = { url: '/media/mkv', at: 0, until: 30, toTimeline: (s) => s };
    const t = setup(40, draft);
    t.draftSlot.loadError = new Error('media error 4');
    t.store.play();
    await t.flush();
    expect(t.store.showing).toBe('still');
    expect(t.store.state).toBe('buffering');
    expect(t.draftSlot.playing).toBe(false);
  });

  it('goes back to paused with a message when the browser blocks play()', async () => {
    const t = setup();
    t.slots[0].playError = new Error('NotAllowedError');
    t.slots[1].playError = new Error('NotAllowedError');
    t.store.play();
    await t.flush();
    await t.finish(0);
    expect(t.store.state).toBe('paused');
    expect(t.store.message).toMatch(/blocked/);
  });

  it('stops with a message when the chunk the playhead needs fails', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    t.reqs[0]!.fail();
    await t.flush();
    await t.flush();
    expect(t.store.state).toBe('paused');
    expect(t.store.message).toMatch(/could not be prepared/);
  });

  it('a cached chunk needs no render wait', async () => {
    const t = setup();
    t.cachedRanges.add(0);
    t.store.play();
    await t.flush();
    await t.flush();
    expect(t.store.state).toBe('playing');
  });

  it('dispose releases every element and blob', async () => {
    const t = setup();
    t.store.play();
    await t.flush();
    await t.finish(0);
    t.store.dispose();
    expect(t.slots[0].src ?? t.slots[1].src).toBeNull();
    expect(t.revoked).toContain('blob:k0');
    expect(t.frames.size).toBe(0);
  });
});
