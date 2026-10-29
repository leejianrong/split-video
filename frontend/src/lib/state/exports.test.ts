import { beforeEach, describe, expect, it, vi } from 'vitest';

const exportCall = vi.fn();
const cancelBatch = vi.fn();
const listJobs = vi.fn();
const getJob = vi.fn();
const cancelJob = vi.fn();
vi.mock('../api/generated/sdk.gen', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/generated/sdk.gen')>()),
  exportModesRouteApiV1ComposeExportPost: (...a: unknown[]) => exportCall(...a),
  renderQueueCancelApiV1ComposeRenderQueueBatchIdCancelPost: (...a: unknown[]) => cancelBatch(...a),
  listJobsApiV1JobsGet: (...a: unknown[]) => listJobs(...a),
  getJobApiV1JobsJobIdGet: (...a: unknown[]) => getJob(...a),
  cancelApiV1JobsJobIdCancelPost: (...a: unknown[]) => cancelJob(...a),
}));

import type { JobOut, ProjectOut } from '../api/client';
import { ExportStore, fixFor, toRow } from './exports.svelte';
import { JobsStore } from './jobs.svelte';
import type { ProjectStore } from './project.svelte';

const job = (over: Partial<JobOut> & { id: string }): JobOut => ({
  kind: 'render',
  state: 'running',
  label: 'Song 1.mp4',
  completed: 0,
  total: 1000,
  fraction: 0,
  created_at: 1,
  group: 'b1',
  ...over,
});

const timeline = {
  name: 'gig.mp4',
  duration: 240,
  tracks: [
    {
      id: 't',
      kind: 'video',
      magnetic: true,
      clips: [
        { id: 'c1', kind: 'media', label: 'Song 1', in_point: 0, out_point: 120, included: true },
        { id: 'c2', kind: 'media', label: 'Song 2', in_point: 120, out_point: 240, included: true },
      ],
    },
  ],
} as unknown as ProjectOut;

function make() {
  const jobs = new JobsStore(() => () => {});
  const project = { id: 'gig.mp4', timeline } as unknown as ProjectStore;
  return { jobs, store: new ExportStore(jobs, project) };
}

beforeEach(() => {
  for (const m of [exportCall, cancelBatch, listJobs, getJob, cancelJob]) m.mockReset();
});

describe('toRow', () => {
  it('maps the job states to the queue states and carries the fix of a failure', () => {
    expect(toRow(job({ id: 'a', state: 'queued' })).state).toBe('waiting');
    expect(toRow(job({ id: 'a', state: 'running', fraction: 0.4 }))).toMatchObject({
      state: 'rendering',
      progress: 0.4,
    });
    const failed = toRow(
      job({
        id: 'a',
        state: 'error',
        error_info: { code: 'source.not_found', message: 'gone', fix: 'Move it back.' },
      }),
    );
    expect(failed).toMatchObject({ state: 'failed', message: 'gone', fix: 'Move it back.' });
  });

  it('says what the dialog can do for a file that exists', () => {
    expect(fixFor('render.output_exists', 'Pass overwrite=true')).toMatch(/Replace files/);
    expect(fixFor('x', 'Use `--flag`')).toBe('Use --flag');
  });
});

describe('ExportStore', () => {
  it('starts the export with the numbers of the included segments and adopts the jobs', async () => {
    const { store, jobs } = make();
    exportCall.mockResolvedValue({
      data: {
        total: 2,
        jobs: [
          { job_id: 'j1', state: 'queued' },
          { job_id: 'j2', state: 'queued' },
        ],
      },
    });
    getJob.mockImplementation(async ({ path }: { path: { job_id: string } }) => ({
      data: job({ id: path.job_id, state: 'queued', label: `${path.job_id}.mp4` }),
    }));
    expect(store.files).toHaveLength(2);
    expect(await store.start()).toBe(true);
    expect(exportCall.mock.calls[0]?.[0].body).toMatchObject({
      mode: 'segments',
      only: [1, 2],
      video: 'gig.mp4',
    });
    expect(store.rows.map((r) => [r.id, r.retryable])).toEqual([
      ['j1', true],
      ['j2', true],
    ]);
    expect(jobs.list).toHaveLength(2);
  });

  it('keeps the engine message and fix when a start is refused', async () => {
    const { store } = make();
    exportCall.mockRejectedValue(new Error('boom'));
    expect(await store.start()).toBe(false);
    expect(store.error?.message).toBe('boom');
  });

  it('retries one file with overwrite and hides the old row', async () => {
    const { store, jobs } = make();
    exportCall.mockResolvedValueOnce({
      data: { total: 2, jobs: [{ job_id: 'j1' }, { job_id: 'j2' }] },
    });
    getJob.mockImplementation(async ({ path }: { path: { job_id: string } }) => ({
      data: job({
        id: path.job_id,
        state: path.job_id === 'j2' ? 'error' : 'done',
        error_info: { code: 'render.output_exists', message: 'exists', fix: 'x' },
      }),
    }));
    await store.start();
    exportCall.mockResolvedValueOnce({ data: { total: 1, jobs: [{ job_id: 'j3' }] } });
    getJob.mockResolvedValue({ data: job({ id: 'j3', state: 'running', created_at: 5 }) });
    expect(await store.retry('j2')).toBe(true);
    expect(exportCall.mock.calls[1]?.[0].body).toMatchObject({ only: [2], overwrite: true });
    expect(store.rows.map((r) => r.id)).not.toContain('j2');
    expect(jobs.byId['j3']).toBeDefined();
  });

  it('cancels one render through the job API and all through the batch', async () => {
    const { store, jobs } = make();
    jobs.ingest(job({ id: 'j1', state: 'running' }));
    jobs.ingest(job({ id: 'j2', state: 'queued', created_at: 2 }));
    cancelJob.mockResolvedValue({ data: job({ id: 'j1', state: 'cancelled' }) });
    await store.cancel('j1');
    expect(cancelJob).toHaveBeenCalledWith({ path: { job_id: 'j1' } });
    expect(jobs.byId['j1']?.state).toBe('cancelled');
    cancelBatch.mockResolvedValue({ data: {} });
    getJob.mockResolvedValue({ data: job({ id: 'j2', state: 'cancelled' }) });
    await store.cancelAll();
    expect(cancelBatch).toHaveBeenCalledWith({ path: { batch_id: 'b1' } });
    expect(store.active).toHaveLength(0);
  });

  it('loads every remembered render so finished rows survive a reload', async () => {
    const { store } = make();
    listJobs.mockResolvedValue({
      data: { jobs: [job({ id: 'j1', state: 'done' }), job({ id: 'j2', state: 'error' })] },
    });
    await store.load();
    expect(listJobs).toHaveBeenCalledWith({ query: { kind: 'render' } });
    expect(store.rows.map((r) => r.state).sort()).toEqual(['done', 'failed']);
  });
});
