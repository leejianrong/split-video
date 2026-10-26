import { beforeEach, describe, expect, it, vi } from 'vitest';

const listJobs = vi.fn();
const getJob = vi.fn();
const cancelJob = vi.fn();
vi.mock('../api/generated/sdk.gen', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/generated/sdk.gen')>()),
  listJobsApiV1JobsGet: (...args: unknown[]) => listJobs(...args),
  getJobApiV1JobsJobIdGet: (...args: unknown[]) => getJob(...args),
  cancelApiV1JobsJobIdCancelPost: (...args: unknown[]) => cancelJob(...args),
}));

import type { EventsOptions, JobOut } from '../api/client';
import { JobsStore, type ProjectEvent } from './jobs.svelte';

const job = (over: Partial<JobOut> & { id: string }): JobOut => ({
  kind: 'split',
  state: 'running',
  label: 'gig.mp4',
  completed: 0,
  total: 1000,
  fraction: 0,
  created_at: 1,
  ...over,
});

/** A stand-in for `connectEvents`: records the options and lets the test push frames. */
function fakeConnect() {
  const state: { options: EventsOptions | null; closed: number; opened: number } = {
    options: null,
    closed: 0,
    opened: 0,
  };
  const connect = (options: EventsOptions) => {
    state.options = options;
    state.opened += 1;
    return () => void (state.closed += 1);
  };
  const frame = (name: Parameters<EventsOptions['onEvent']>[0], data: unknown) =>
    state.options?.onEvent(name, data, '');
  const status = (s: Parameters<NonNullable<EventsOptions['onStatus']>>[0], reconnect = false) =>
    state.options?.onStatus?.(s, { reconnect, attempt: 0 });
  return { state, connect, frame, status };
}

beforeEach(() => {
  listJobs.mockReset();
  getJob.mockReset();
  cancelJob.mockReset();
});

describe('JobsStore', () => {
  it('owns one connection: start twice opens once, stop closes it', () => {
    const f = fakeConnect();
    const store = new JobsStore(f.connect);
    store.start();
    store.start();
    expect(f.state.opened).toBe(1);
    f.status('open');
    expect(store.status).toBe('open');
    store.stop();
    expect(f.state.closed).toBe(1);
  });

  it('fills from hello and job frames and sorts newest first', () => {
    const f = fakeConnect();
    const store = new JobsStore(f.connect);
    store.start();
    f.frame('hello', { jobs: [job({ id: 'a', created_at: 1 })], resync: false });
    f.frame('job', { type: 'submitted', job: job({ id: 'b', created_at: 5 }) });
    f.frame('job', { type: 'progress', job: job({ id: 'b', created_at: 5, fraction: 0.4 }) });
    expect(store.list.map((j) => j.id)).toEqual(['b', 'a']);
    expect(store.byId.b?.fraction).toBe(0.4);
  });

  it('tells subscribers once when a job it saw running finishes, and never moves it back', () => {
    const f = fakeConnect();
    const store = new JobsStore(f.connect);
    const done: string[] = [];
    store.onFinished((j) => done.push(`${j.id}:${j.state}`));
    store.start();
    f.frame('job', { type: 'submitted', job: job({ id: 'a' }) });
    f.frame('job', { type: 'state', job: job({ id: 'a', state: 'done', fraction: 1 }) });
    f.frame('job', { type: 'state', job: job({ id: 'a', state: 'done', fraction: 1 }) });
    f.frame('job', { type: 'progress', job: job({ id: 'a', state: 'running' }) }); // late, out of order
    expect(done).toEqual(['a:done']);
    expect(store.byId.a?.state).toBe('done');
  });

  it('ignores a job that was only ever seen finished, unless it is watched', () => {
    const store = new JobsStore(fakeConnect().connect);
    const done: string[] = [];
    store.onFinished((j) => done.push(j.id));
    store.ingest(job({ id: 'old', state: 'done' }));
    store.watch('mine');
    store.ingest(job({ id: 'mine', state: 'error' }));
    expect(done).toEqual(['mine']);
  });

  it('refetches the list and calls resync listeners when the stream comes back', async () => {
    const f = fakeConnect();
    const store = new JobsStore(f.connect);
    const resynced = vi.fn();
    store.onResync(resynced);
    store.start();
    listJobs.mockResolvedValue({ data: { jobs: [job({ id: 'x', state: 'done' })] } });
    f.status('open', false);
    expect(listJobs).not.toHaveBeenCalled(); // the first open needs no resync
    f.status('reconnecting', true);
    expect(store.status).toBe('reconnecting');
    f.status('open', true);
    await vi.waitFor(() => expect(resynced).toHaveBeenCalledTimes(1));
    expect(listJobs).toHaveBeenCalledTimes(1);
    expect(store.byId.x?.state).toBe('done');
  });

  it('asks listeners to resync when hello says the replay was lost', () => {
    const f = fakeConnect();
    const store = new JobsStore(f.connect);
    const resynced = vi.fn();
    store.onResync(resynced);
    store.start();
    f.frame('hello', { jobs: [], resync: true });
    expect(resynced).toHaveBeenCalledTimes(1);
  });

  it('passes project events on', () => {
    const f = fakeConnect();
    const store = new JobsStore(f.connect);
    const seen: ProjectEvent[] = [];
    store.onProject((e) => seen.push(e));
    store.start();
    f.frame('project', { kind: 'split', project_id: 'gig.mp4', revision: 4, source: 'external' });
    expect(seen).toEqual([
      { kind: 'split', project_id: 'gig.mp4', revision: 4, source: 'external' },
    ]);
  });

  it('cancels through the API and takes the answer', async () => {
    const store = new JobsStore(fakeConnect().connect);
    store.ingest(job({ id: 'a' }));
    cancelJob.mockResolvedValue({ data: job({ id: 'a', state: 'cancelled' }) });
    const result = await store.cancel('a');
    expect(cancelJob).toHaveBeenCalledWith({ path: { job_id: 'a' } });
    expect(result.state).toBe('cancelled');
    expect(store.byId.a?.state).toBe('cancelled');
  });

  it('refresh fetches one job, and returns null when that fails', async () => {
    const store = new JobsStore(fakeConnect().connect);
    getJob.mockResolvedValueOnce({ data: job({ id: 'a', fraction: 0.2 }) });
    expect((await store.refresh('a'))?.fraction).toBe(0.2);
    getJob.mockRejectedValueOnce(new Error('down'));
    expect(await store.refresh('a')).toBeNull();
  });

  it('finds the latest job of a kind by label', () => {
    const store = new JobsStore(fakeConnect().connect);
    store.ingest(job({ id: 'a', kind: 'proxy', label: 'x.mp4', created_at: 1 }));
    store.ingest(job({ id: 'b', kind: 'proxy', label: 'x.mp4', created_at: 2 }));
    store.ingest(job({ id: 'c', kind: 'split', label: 'x.mp4', created_at: 3 }));
    expect(store.latest('proxy', 'x.mp4')?.id).toBe('b');
    expect(store.latest('proxy', 'y.mp4')).toBeUndefined();
  });
});
