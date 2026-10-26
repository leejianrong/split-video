import { beforeEach, describe, expect, it, vi } from 'vitest';

const list = vi.fn();
const register = vi.fn();
const makeProxy = vi.fn();
const getAsset = vi.fn();
const getJob = vi.fn();
vi.mock('../api/generated/sdk.gen', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/generated/sdk.gen')>()),
  listAssetsApiV1AssetsGet: (...a: unknown[]) => list(...a),
  registerAssetApiV1AssetsRegisterPost: (...a: unknown[]) => register(...a),
  makeProxyApiV1AssetsAssetIdProxyPost: (...a: unknown[]) => makeProxy(...a),
  getAssetApiV1AssetsAssetIdGet: (...a: unknown[]) => getAsset(...a),
  getJobApiV1JobsJobIdGet: (...a: unknown[]) => getJob(...a),
}));

import { ApiError, configureClient, type AssetOut, type JobOut } from '../api/client';
import { AssetsStore, posterTime, posterUrl } from './assets.svelte';
import { JobsStore } from './jobs.svelte';

const asset = (over: Partial<AssetOut> & { id: string }): AssetOut =>
  ({
    path: `${over.id}.mp4`,
    size: 1,
    mtime: 1,
    duration: 100,
    has_audio: true,
    has_video: true,
    width: 640,
    height: 360,
    fps: 25,
    copies: [],
    present: true,
    proxy: false,
    thumbnails: false,
    transcript: false,
    scenes: false,
    ocr: false,
    ...over,
  }) as AssetOut;

const job = (over: Partial<JobOut> & { id: string }): JobOut =>
  ({
    kind: 'proxy',
    state: 'running',
    label: 'a.mp4',
    completed: 0,
    total: 4,
    fraction: 0,
    created_at: 1,
    ...over,
  }) as JobOut;

function setup(uploader = vi.fn()) {
  const jobs = new JobsStore(() => () => undefined);
  const store = new AssetsStore(jobs, uploader);
  return { jobs, store, uploader };
}

beforeEach(() => {
  for (const m of [list, register, makeProxy, getAsset, getJob]) m.mockReset();
  configureClient({ baseUrl: 'http://localhost', token: null });
  makeProxy.mockResolvedValue({ data: { job_id: 'p1' } });
  getJob.mockResolvedValue({ data: job({ id: 'p1', label: 'a.mp4' }) });
});

describe('poster frames', () => {
  it('picks a time a little way in, never past the end', () => {
    expect(posterTime(100)).toBe(10);
    expect(posterTime(9668)).toBe(30);
    expect(posterTime(0.5)).toBeCloseTo(0.05, 5);
    expect(posterTime(0)).toBe(0);
  });

  it('is a small frame request for a video that is there, and nothing for audio or a missing file', () => {
    const url = new URL(posterUrl(asset({ id: 'a_1', duration: 100 })) ?? '');
    expect(url.pathname).toBe('/api/v1/assets/a_1/frame');
    expect(url.searchParams.get('time')).toBe('10');
    expect(url.searchParams.get('width')).toBe('240');
    expect(posterUrl(asset({ id: 'a_2', has_video: false }))).toBeNull();
    expect(posterUrl(asset({ id: 'a_3', present: false }))).toBeNull();
  });
});

describe('AssetsStore library', () => {
  it('loads the table, and keeps the catalogue error when that fails', async () => {
    const { store } = setup();
    list.mockResolvedValueOnce({ data: { assets: [asset({ id: 'a' })] } });
    await store.load();
    expect(store.status).toBe('ready');
    expect(store.items.map((a) => a.id)).toEqual(['a']);
    list.mockRejectedValueOnce(
      new ApiError({ code: 'network', message: 'down', fix: 'Retry', status: 0 }),
    );
    await store.load();
    expect(store.status).toBe('error');
    expect(store.error).toMatchObject({ code: 'network', fix: 'Retry' });
  });

  it('registers a file from the recordings folder, prepares its preview, and does not duplicate it', async () => {
    const { store, jobs } = setup();
    register.mockResolvedValue({ data: asset({ id: 'a', path: 'a.mp4' }) });
    await store.register('a.mp4');
    await store.register('a.mp4');
    expect(store.items).toHaveLength(1);
    expect(register).toHaveBeenCalledWith({ body: { path: 'a.mp4' } });
    await vi.waitFor(() => expect(makeProxy).toHaveBeenCalled());
    expect(makeProxy.mock.calls[0]?.[0]).toMatchObject({ path: { asset_id: 'a' } });
    await vi.waitFor(() => expect(jobs.byId.p1).toBeDefined());
  });

  it('prepares nothing for audio, for a file with a proxy, or when told not to; a refused proxy is not fatal', async () => {
    const { store } = setup();
    await store.prepare(asset({ id: 'x', has_video: false }));
    await store.prepare(asset({ id: 'y', proxy: true }));
    expect(makeProxy).not.toHaveBeenCalled();
    register.mockResolvedValue({ data: asset({ id: 'z', path: 'z.mp4' }) });
    await store.register('z.mp4', { prepare: false });
    expect(makeProxy).not.toHaveBeenCalled();
    makeProxy.mockRejectedValueOnce(new Error('busy'));
    await expect(store.prepare(asset({ id: 'w', path: 'w.mp4' }))).resolves.toBeUndefined();
  });

  it('shows the preview percent while a proxy job runs, and the missing state', () => {
    const { store, jobs } = setup();
    const a = asset({ id: 'a', path: 'a.mp4' });
    expect(store.cardOf(a)).toEqual({ kind: 'ready', preparing: null });
    jobs.ingest(job({ id: 'p', label: 'a.mp4', fraction: 0.724 }));
    expect(store.cardOf(a)).toEqual({ kind: 'ready', preparing: 72 });
    jobs.ingest(job({ id: 'p', label: 'a.mp4', state: 'done', fraction: 1 }));
    expect(store.cardOf(a).preparing).toBeNull();
    expect(store.cardOf(asset({ id: 'm', present: false })).kind).toBe('missing');
  });

  it('reloads an asset when its proxy finishes', async () => {
    const { store, jobs } = setup();
    store.upsert(asset({ id: 'a', path: 'a.mp4' }));
    getAsset.mockResolvedValue({ data: asset({ id: 'a', path: 'a.mp4', proxy: true }) });
    jobs.ingest(job({ id: 'p', label: 'a.mp4' }));
    jobs.ingest(job({ id: 'p', label: 'a.mp4', state: 'done', fraction: 1 }));
    await vi.waitFor(() => expect(store.byId('a')?.proxy).toBe(true));
  });

  it('ensure() registers the open recording once, quietly', async () => {
    const { store } = setup();
    register.mockResolvedValue({ data: asset({ id: 'a', path: 'gig.mp4' }) });
    await store.ensure('gig.mp4');
    await store.ensure('gig.mp4');
    expect(register).toHaveBeenCalledTimes(1);
    expect(makeProxy).not.toHaveBeenCalled();
    register.mockRejectedValue(new Error('x'));
    await expect(store.ensure('other.mp4')).resolves.toBeUndefined();
  });

  it('locate() says whether the file was found again', async () => {
    const { store } = setup();
    store.upsert(asset({ id: 'a', present: false }));
    getAsset.mockResolvedValueOnce({ data: asset({ id: 'a', present: false }) });
    expect(await store.locate('a')).toBe(false);
    getAsset.mockResolvedValueOnce({ data: asset({ id: 'a', present: true }) });
    expect(await store.locate('a')).toBe(true);
    expect(store.byId('a')?.present).toBe(true);
  });
});

describe('AssetsStore uploads', () => {
  const file = (name: string) => new File(['x'.repeat(10)], name);

  it('uploads one file after the other, reports progress, and turns each into an asset', async () => {
    const seen: string[] = [];
    const uploader = vi.fn(async (f: File, o: { onProgress?: (l: number, t: number) => void }) => {
      seen.push(f.name);
      o.onProgress?.(5, 10);
      return asset({ id: f.name, path: f.name });
    });
    const { store } = setup(uploader);
    store.enqueue([file('one.mp4'), file('two.mp4')]);
    expect(store.uploads.map((u) => u.name)).toEqual(['one.mp4', 'two.mp4']);
    await vi.waitFor(() => expect(store.items).toHaveLength(2));
    expect(seen).toEqual(['one.mp4', 'two.mp4']);
    expect(store.uploads).toEqual([]);
    await vi.waitFor(() => expect(makeProxy).toHaveBeenCalledTimes(2));
  });

  it('keeps a failed upload with its fix hint, and retries it', async () => {
    const uploader = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError({
          code: 'upload.unsupported_type',
          message: 'not media',
          fix: 'Use a video.',
          status: 415,
        }),
      )
      .mockResolvedValueOnce(asset({ id: 'ok', path: 'notes.mp4' }));
    const { store } = setup(uploader);
    store.enqueue([file('notes.mp4')]);
    await vi.waitFor(() => expect(store.uploads[0]?.status).toBe('failed'));
    expect(store.uploads[0]?.error).toMatchObject({
      code: 'upload.unsupported_type',
      fix: 'Use a video.',
    });
    store.retry(store.uploads[0]!.id);
    await vi.waitFor(() => expect(store.items).toHaveLength(1));
    expect(store.uploads).toEqual([]);
  });

  it('cancelling an upload in flight aborts it and removes the card; a waiting one is just dropped', async () => {
    let signal: AbortSignal | undefined;
    const uploader = vi.fn(
      (_f: File, o: { signal?: AbortSignal }) =>
        new Promise<AssetOut>((_resolve, reject) => {
          signal = o.signal;
          o.signal?.addEventListener('abort', () =>
            reject(new ApiError({ code: 'cancelled', message: 'cancelled', status: 0 })),
          );
        }),
    );
    const { store } = setup(uploader);
    store.enqueue([file('big.mp4'), file('next.mp4')]);
    await vi.waitFor(() => expect(store.uploads[0]?.status).toBe('uploading'));
    store.cancelUpload(store.uploads[1]!.id);
    expect(store.uploads.map((u) => u.name)).toEqual(['big.mp4']);
    store.cancelUpload(store.uploads[0]!.id);
    expect(signal?.aborted).toBe(true);
    await vi.waitFor(() => expect(store.uploads).toEqual([]));
    expect(store.items).toEqual([]);
  });
});
