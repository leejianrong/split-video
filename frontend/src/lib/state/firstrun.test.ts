import { describe, expect, it, vi } from 'vitest';
import type { AssetOut, JobOut } from '../api/client';
import type { AssetsStore } from './assets.svelte';
import { FirstRunStore } from './firstrun.svelte';
import { JobsStore } from './jobs.svelte';
import type { QuickActionsStore, RunView } from './quickActions.svelte';

const asset = (over: Partial<AssetOut> = {}): AssetOut =>
  ({
    id: 'a_1',
    path: 'gig.mp4',
    duration: 9668,
    has_video: true,
    has_audio: true,
    present: true,
    proxy: false,
    ...over,
  }) as AssetOut;

const job = (over: Partial<JobOut>): JobOut =>
  ({
    id: 'j1',
    kind: 'proxy',
    label: 'gig.mp4',
    state: 'running',
    fraction: 0.72,
    total: 4,
    completed: 3,
    created_at: 1,
    ...over,
  }) as JobOut;

function setup() {
  const jobs = new JobsStore(() => () => undefined);
  const lib = { byId: (id: string) => (id === 'a_1' ? asset() : undefined), prepare: vi.fn() };
  const view = vi.fn((): RunView => ({
    state: 'idle',
    fraction: null,
    message: null,
    error: null,
    summary: null,
    cancelling: false,
  }));
  const quick = { run: vi.fn(async () => true), dismiss: vi.fn(), view };
  const store = new FirstRunStore(
    lib as unknown as AssetsStore,
    jobs,
    quick as unknown as QuickActionsStore,
  );
  return { jobs, lib, quick, store, view };
}

describe('FirstRunStore', () => {
  it('starts the goal once both a recording and a goal are set, in either order', async () => {
    const a = setup();
    a.store.choose('songs');
    expect(a.quick.run).not.toHaveBeenCalled();
    a.store.setAsset(asset());
    await vi.waitFor(() => expect(a.quick.run).toHaveBeenCalledWith('songs', asset()));

    const b = setup();
    b.store.setAsset(asset());
    expect(b.quick.run).not.toHaveBeenCalled();
    b.store.choose('highlights');
    await vi.waitFor(() => expect(b.quick.run).toHaveBeenCalledWith('highlights', asset()));
  });

  it('runs nothing for an empty timeline, transcribes only for shorts or when asked, and locks the goal once started', async () => {
    const e = setup();
    e.store.setAsset(asset());
    e.store.choose('empty');
    expect(e.quick.run).not.toHaveBeenCalled();
    expect(e.store.started).toBe(true);
    e.store.choose('songs');
    expect(e.store.goal).toBe('empty');

    const s = setup();
    s.store.setAsset(asset());
    s.store.choose('shorts');
    await vi.waitFor(() => expect(s.quick.run).toHaveBeenCalledTimes(2));
    expect(s.quick.run).toHaveBeenLastCalledWith('transcribe', asset());
  });

  it('reads the preview step from the proxy job and lets the editor open when it is usable', () => {
    const { store, jobs } = setup();
    store.setAsset(asset());
    expect(store.steps.map((s) => [s.id, s.state])).toEqual([['preview', 'waiting']]);
    expect(store.canOpen).toBe(false);
    jobs.ingest(job({}));
    expect(store.steps[0]).toMatchObject({ state: 'working', fraction: 0.72 });
    jobs.ingest(job({ state: 'done', fraction: 1 }));
    expect(store.steps[0]?.state).toBe('done');
    expect(store.canOpen).toBe(true);
  });

  it('lets the editor open when the preview failed, and offers Retry', () => {
    const { store, jobs } = setup();
    store.setAsset(asset());
    jobs.ingest(job({ state: 'error', error: 'ffmpeg failed' }));
    expect(store.steps[0]).toMatchObject({ state: 'failed' });
    expect(store.canOpen).toBe(true);
  });

  it('shows the goal step with the run state, and retries it', async () => {
    const { store, view, quick } = setup();
    store.setAsset(asset());
    store.choose('songs');
    view.mockReturnValue({
      state: 'done',
      fraction: 1,
      message: null,
      error: null,
      summary: '3 songs found',
      cancelling: false,
    });
    const goal = store.steps.find((s) => s.id === 'analysis');
    expect(goal).toMatchObject({ state: 'done', summary: '3 songs found' });
    expect(goal?.label).toBe('Listening for silences and music');
    await store.retry('analysis');
    expect(quick.dismiss).toHaveBeenCalledWith('a_1', 'songs');
  });
});
