import { beforeEach, describe, expect, it, vi } from 'vitest';

const split = vi.fn();
const prepareReport = vi.fn();
const transcribe = vi.fn();
const highlights = vi.fn();
const getJob = vi.fn();
const cancelJob = vi.fn();
const open = vi.fn();
const timeline = vi.fn();
vi.mock('../api/generated/sdk.gen', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/generated/sdk.gen')>()),
  startSplitApiV1AssetsAssetIdSplitPost: (...a: unknown[]) => split(...a),
  prepareReportApiV1AssetsAssetIdReportPreparePost: (...a: unknown[]) => prepareReport(...a),
  startTranscriptionApiV1AssetsAssetIdTranscribePost: (...a: unknown[]) => transcribe(...a),
  getHighlightsApiV1AssetsAssetIdHighlightsGet: (...a: unknown[]) => highlights(...a),
  getJobApiV1JobsJobIdGet: (...a: unknown[]) => getJob(...a),
  cancelApiV1JobsJobIdCancelPost: (...a: unknown[]) => cancelJob(...a),
  openFileApiV1OpenPost: (...a: unknown[]) => open(...a),
  getVideoTimelineApiV1ProjectTimelineGet: (...a: unknown[]) => timeline(...a),
}));

import { ApiError, type AssetOut, type JobOut } from '../api/client';
import { JobsStore } from './jobs.svelte';
import { ProjectStore } from './project.svelte';
import { jobError, QuickActionsStore } from './quickActions.svelte';

const asset = {
  id: 'a_1',
  path: 'gig.mp4',
  has_audio: true,
  has_video: true,
  present: true,
} as AssetOut;

const job = (over: Partial<JobOut> & { id: string }): JobOut =>
  ({
    kind: 'split',
    state: 'running',
    label: 'gig.mp4',
    completed: 0,
    total: 1000,
    fraction: 0,
    created_at: 1,
    ...over,
  }) as JobOut;

const stateOf = (revision: number) => ({
  data: {
    filename: 'gig.mp4',
    duration: 100,
    video_url: '',
    params: {},
    silences: [],
    segments: [],
    resumed: true,
    revision,
  },
});

async function setup() {
  const jobs = new JobsStore(() => () => undefined);
  const project = new ProjectStore();
  open.mockResolvedValue(stateOf(3));
  timeline.mockResolvedValue({ data: { name: 'gig.mp4', revision: 3, duration: 100, tracks: [] } });
  await project.open('gig.mp4');
  const store = new QuickActionsStore(jobs, project);
  return { jobs, project, store };
}

beforeEach(() => {
  for (const m of [split, prepareReport, transcribe, highlights, getJob, cancelJob, open, timeline])
    m.mockReset();
});

describe('QuickActionsStore', () => {
  it('starts split-by-silence with the open project revision and shows the job progress', async () => {
    const { store, jobs } = await setup();
    split.mockResolvedValue({ data: { job_id: 'j1' } });
    getJob.mockResolvedValue({ data: job({ id: 'j1', fraction: 0.25 }) });
    expect(await store.run('split', asset)).toBe(true);
    expect(split).toHaveBeenCalledWith({
      path: { asset_id: 'a_1' },
      body: { label_prefix: 'Part' },
      headers: { 'If-Match': '"3"' },
    });
    expect(store.view('a_1', 'split')).toMatchObject({ state: 'running', fraction: 0.25 });
    expect(store.isBusy('a_1', 'split')).toBe(true);
    // a second press while it runs does nothing
    expect(await store.run('split', asset)).toBe(false);
    expect(split).toHaveBeenCalledTimes(1);
    jobs.ingest(job({ id: 'j1', fraction: 0.6, message: 'Listening for silence' }));
    expect(store.view('a_1', 'split')).toMatchObject({
      fraction: 0.6,
      message: 'Listening for silence',
    });
  });

  it('sends the Song label for Find songs, and no revision for an asset that is not the open project', async () => {
    const { store } = await setup();
    split.mockResolvedValue({ data: { job_id: 'j1' } });
    getJob.mockResolvedValue({ data: job({ id: 'j1' }) });
    await store.run('songs', { ...asset, id: 'a_2', path: 'other.mp4' });
    expect(split.mock.calls[0]?.[0]).toEqual({
      path: { asset_id: 'a_2' },
      body: { label_prefix: 'Song' },
    });
  });

  it('on done refreshes the open project and says what it found', async () => {
    const { store, jobs, project } = await setup();
    split.mockResolvedValue({ data: { job_id: 'j1' } });
    getJob.mockResolvedValue({ data: job({ id: 'j1' }) });
    await store.run('songs', asset);
    open.mockResolvedValue(stateOf(4));
    timeline.mockResolvedValue({
      data: { name: 'gig.mp4', revision: 4, duration: 100, tracks: [] },
    });
    jobs.ingest(
      job({ id: 'j1', state: 'done', fraction: 1, result: { segments: 3, revision: 4 } }),
    );
    await vi.waitFor(() => expect(project.revision).toBe(4));
    expect(store.view('a_1', 'songs')).toMatchObject({ state: 'done', summary: '3 songs found' });
  });

  it('says so when there was nothing to cut at', async () => {
    const { store, jobs } = await setup();
    split.mockResolvedValue({ data: { job_id: 'j1' } });
    getJob.mockResolvedValue({ data: job({ id: 'j1' }) });
    await store.run('split', asset);
    jobs.ingest(job({ id: 'j1', state: 'done', result: { segments: 1 } }));
    await vi.waitFor(() =>
      expect(store.view('a_1', 'split').summary).toBe('No silence long enough to cut at'),
    );
  });

  it('shows a failed job with the catalogue fix, and Retry runs it again', async () => {
    const { store, jobs } = await setup();
    split.mockResolvedValue({ data: { job_id: 'j1' } });
    getJob.mockResolvedValue({ data: job({ id: 'j1' }) });
    await store.run('split', asset);
    jobs.ingest(
      job({
        id: 'j1',
        state: 'error',
        error: 'boom',
        error_info: {
          code: 'revision.conflict',
          message: 'The project changed.',
          fix: 'Reload and retry.',
        },
      }),
    );
    expect(store.view('a_1', 'split')).toMatchObject({
      state: 'error',
      error: {
        code: 'revision.conflict',
        message: 'The project changed.',
        fix: 'Reload and retry.',
      },
    });
    expect(store.isBusy('a_1', 'split')).toBe(false);
    split.mockResolvedValue({ data: { job_id: 'j2' } });
    getJob.mockResolvedValue({ data: job({ id: 'j2' }) });
    expect(await store.run('split', asset)).toBe(true);
    expect(store.view('a_1', 'split').state).toBe('running');
  });

  it('keeps the refusal of a request (the transcription add-on is not installed) with its fix', async () => {
    const { store } = await setup();
    transcribe.mockRejectedValue(
      new ApiError({
        code: 'extra.transcription_missing',
        message: 'Transcription is not installed.',
        fix: "Install it with `pip install 'split-video[transcribe]'`.",
        status: 501,
      }),
    );
    expect(await store.run('transcribe', asset)).toBe(false);
    const view = store.view('a_1', 'transcribe');
    expect(view.state).toBe('error');
    expect(view.error).toMatchObject({ code: 'extra.transcription_missing', status: 501 });
    expect(view.error?.fix).toContain('split-video[transcribe]');
    store.dismiss('a_1', 'transcribe');
    expect(store.view('a_1', 'transcribe').state).toBe('idle');
  });

  it('cancels through the jobs API and shows Cancelled', async () => {
    const { store } = await setup();
    split.mockResolvedValue({ data: { job_id: 'j1' } });
    getJob.mockResolvedValue({ data: job({ id: 'j1' }) });
    await store.run('split', asset);
    cancelJob.mockResolvedValue({ data: job({ id: 'j1', state: 'cancelled' }) });
    await store.cancel('a_1', 'split');
    expect(cancelJob).toHaveBeenCalledWith({ path: { job_id: 'j1' } });
    expect(store.view('a_1', 'split').state).toBe('cancelled');
  });

  it('Find highlights prepares the signals, then reads the candidates', async () => {
    const { store, jobs } = await setup();
    prepareReport.mockResolvedValue({ data: { job_id: 'j1' } });
    getJob.mockResolvedValue({ data: job({ id: 'j1', kind: 'analysis' }) });
    await store.run('highlights', asset);
    highlights.mockResolvedValue({ data: { candidates: [{}, {}, {}, {}] } });
    jobs.ingest(job({ id: 'j1', kind: 'analysis', state: 'done', fraction: 1 }));
    await vi.waitFor(() =>
      expect(store.view('a_1', 'highlights').summary).toBe('4 highlights found'),
    );
    expect(highlights).toHaveBeenCalledWith({ path: { asset_id: 'a_1' } });
  });

  it('turns "no signals stored yet" into an error with its fix', async () => {
    const { store, jobs } = await setup();
    prepareReport.mockResolvedValue({ data: { job_id: 'j1' } });
    getJob.mockResolvedValue({ data: job({ id: 'j1', kind: 'analysis' }) });
    await store.run('highlights', asset);
    highlights.mockRejectedValue(
      new ApiError({
        code: 'analysis.missing',
        message: 'nothing to score yet',
        fix: 'Run Transcribe first.',
        status: 409,
      }),
    );
    jobs.ingest(job({ id: 'j1', kind: 'analysis', state: 'done' }));
    await vi.waitFor(() => expect(store.view('a_1', 'highlights').state).toBe('error'));
    expect(store.view('a_1', 'highlights').error?.fix).toBe('Run Transcribe first.');
  });

  it('jobError reads error_info, then the bare fields', () => {
    expect(jobError(job({ id: 'x', error: 'plain', error_code: 'RuntimeError' }))).toMatchObject({
      code: 'RuntimeError',
      message: 'plain',
      fix: '',
    });
  });
});
