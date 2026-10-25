import { beforeEach, describe, expect, it, vi } from 'vitest';

const open = vi.fn();
vi.mock('../api/generated/sdk.gen', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/generated/sdk.gen')>()),
  openFileApiV1OpenPost: (...args: unknown[]) => open(...args),
}));

import { ApiError } from '../api/client';
import { ProjectStore } from './project.svelte';

const state = (filename: string, revision = 3) => ({
  data: {
    filename,
    duration: 9668,
    video_url: '',
    params: {},
    silences: [],
    segments: [],
    resumed: true,
    revision,
  },
});

beforeEach(() => {
  open.mockReset();
});

describe('ProjectStore', () => {
  it('loads a project through /open and keeps its revision', async () => {
    open.mockResolvedValue(state('gig.mp4'));
    const store = new ProjectStore();
    expect(store.status).toBe('idle');
    const loading = store.open('gigs/gig.mp4');
    expect(store.status).toBe('loading');
    await loading;
    expect(open).toHaveBeenCalledWith({ body: { path: 'gigs/gig.mp4' } });
    expect(store.status).toBe('ready');
    expect(store.name).toBe('gig.mp4');
    expect(store.revision).toBe(3);
    expect(store.id).toBe('gigs/gig.mp4');
  });

  it('keeps the catalogue error and fix hint when the project cannot be opened', async () => {
    open.mockImplementation(() =>
      Promise.reject(
        new ApiError({
          code: 'media.not_found',
          message: 'No such file',
          fix: 'Pick another',
          status: 404,
        }),
      ),
    );
    const store = new ProjectStore();
    await store.open('missing.mp4');
    expect(store.status).toBe('error');
    expect(store.project).toBeNull();
    expect(store.error).toEqual({
      code: 'media.not_found',
      message: 'No such file',
      fix: 'Pick another',
      status: 404,
    });
  });

  it('turns an unexpected failure into an unknown error', async () => {
    open.mockImplementation(() => Promise.reject(new Error('boom')));
    const store = new ProjectStore();
    await store.open('x.mp4');
    expect(store.error).toMatchObject({ code: 'unknown', message: 'boom' });
  });

  it('lets only the newest open() write, whatever order the answers arrive in', async () => {
    let finishFirst: (value: unknown) => void = () => {};
    open.mockImplementationOnce(() => new Promise((resolve) => (finishFirst = resolve)));
    open.mockResolvedValueOnce(state('second.mp4', 7));
    const store = new ProjectStore();
    const first = store.open('first.mp4');
    await store.open('second.mp4');
    finishFirst(state('first.mp4', 1));
    await first;
    expect(store.id).toBe('second.mp4');
    expect(store.name).toBe('second.mp4');
    expect(store.revision).toBe(7);
  });

  it('a failed reload clears the old project, and close() resets everything', async () => {
    open.mockResolvedValueOnce(state('a.mp4'));
    const store = new ProjectStore();
    await store.open('a.mp4');
    open.mockImplementationOnce(() => Promise.reject(new Error('gone')));
    await store.open('b.mp4');
    expect(store.project).toBeNull();
    store.close();
    expect(store.status).toBe('idle');
    expect(store.id).toBeNull();
    expect(store.error).toBeNull();
  });
});
