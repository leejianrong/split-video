import { beforeEach, describe, expect, it, vi } from 'vitest';

const open = vi.fn();
const timeline = vi.fn();
const addClip = vi.fn();
const applyOps = vi.fn();
vi.mock('../api/generated/sdk.gen', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/generated/sdk.gen')>()),
  openFileApiV1OpenPost: (...args: unknown[]) => open(...args),
  getVideoTimelineApiV1ProjectTimelineGet: (...args: unknown[]) => timeline(...args),
  addAssetClipApiV1ProjectClipsPost: (...args: unknown[]) => addClip(...args),
  applyVideoOpsApiV1ProjectOpsPost: (...args: unknown[]) => applyOps(...args),
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

const clipOf = (id: string, source: string) => ({ id, source, asset: `a_${id}`, duration: 10 });
const timelineOf = (revision: number, sources: string[] = ['gig.mp4']) => ({
  data: {
    name: 'gig.mp4',
    revision,
    duration: 10 * sources.length,
    tracks: [
      { id: 't1', kind: 'video', magnetic: true, clips: sources.map((s, i) => clipOf(`c${i}`, s)) },
    ],
  },
});

beforeEach(() => {
  open.mockReset();
  timeline.mockReset();
  addClip.mockReset();
  applyOps.mockReset();
  timeline.mockResolvedValue(timelineOf(3));
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

  describe('timeline and edits', () => {
    const openStore = async () => {
      open.mockResolvedValue(state('gig.mp4', 3));
      const store = new ProjectStore();
      await store.open('gig.mp4');
      return store;
    };

    it('reads the timeline with the project, and survives the timeline failing', async () => {
      const store = await openStore();
      expect(timeline).toHaveBeenCalledWith({ query: { path: 'gig.mp4' } });
      expect(store.timeline?.tracks[0]?.clips).toHaveLength(1);
      timeline.mockRejectedValue(new Error('nope'));
      open.mockResolvedValue(state('gig.mp4', 3));
      const other = new ProjectStore();
      await other.open('gig.mp4');
      expect(other.status).toBe('ready');
      expect(other.timeline).toBeNull();
      expect(other.revision).toBe(3);
    });

    it('adds an asset with If-Match and takes the new revision and timeline', async () => {
      const store = await openStore();
      addClip.mockResolvedValue(timelineOf(4, ['gig.mp4', 'intro.mp4']));
      expect(await store.addAsset('a_9')).toBe(true);
      const call = addClip.mock.calls[0]?.[0];
      expect(call).toMatchObject({
        query: { path: 'gig.mp4' },
        body: { asset: 'a_9', index: null },
        headers: { 'If-Match': '"3"' },
      });
      expect(store.revision).toBe(4);
      expect(store.project?.revision).toBe(4);
      expect(store.timeline?.tracks[0]?.clips.map((c) => c.source)).toEqual([
        'gig.mp4',
        'intro.mp4',
      ]);
      expect(store.editError).toBeNull();
    });

    it('runs edits one at a time, each from the revision the last one returned', async () => {
      const store = await openStore();
      const order: string[] = [];
      let finishFirst: (value: unknown) => void = () => {};
      addClip.mockImplementationOnce(() => {
        order.push('first started');
        return new Promise((resolve) => (finishFirst = resolve));
      });
      addClip.mockImplementationOnce(() => {
        order.push('second started');
        return Promise.resolve(timelineOf(5));
      });
      const first = store.addAsset('a_1');
      const second = store.addAsset('a_2');
      await Promise.resolve();
      expect(order).toEqual(['first started']);
      finishFirst(timelineOf(4));
      await Promise.all([first, second]);
      expect(order).toEqual(['first started', 'second started']);
      expect(addClip.mock.calls[1]?.[0].headers['If-Match']).toBe('"4"');
      expect(store.revision).toBe(5);
    });

    it('on a 409 reloads the project, says it changed elsewhere and does not re-apply', async () => {
      const store = await openStore();
      addClip.mockImplementation(() =>
        Promise.reject(
          new ApiError({
            code: 'revision.conflict',
            message: 'stale',
            fix: 'reload',
            status: 409,
            currentRevision: 6,
          }),
        ),
      );
      open.mockResolvedValue(state('gig.mp4', 6));
      timeline.mockResolvedValue(timelineOf(6, ['gig.mp4', 'agent-added.mp4']));
      expect(await store.addAsset('a_9')).toBe(false);
      expect(addClip).toHaveBeenCalledTimes(1);
      expect(store.editError?.message).toMatch(/changed somewhere else/);
      expect(store.editError?.fix).toMatch(/Fix:/);
      expect(store.revision).toBe(6);
      expect(store.timeline?.tracks[0]?.clips).toHaveLength(2);
    });

    it('keeps the catalogue fix when the server refuses the edit', async () => {
      const store = await openStore();
      addClip.mockImplementation(() =>
        Promise.reject(
          new ApiError({
            code: 'timeline.invalid',
            message: 'no video stream',
            fix: 'Use a video.',
            status: 422,
          }),
        ),
      );
      expect(await store.addAsset('a_audio')).toBe(false);
      expect(store.editError).toMatchObject({ code: 'timeline.invalid', fix: 'Use a video.' });
      expect(store.revision).toBe(3);
    });

    it('reloads on a project event for it that is newer, and ignores old or foreign ones', async () => {
      const store = await openStore();
      open.mockClear();
      expect(
        await store.handleProjectEvent({
          kind: 'split',
          project_id: 'gig.mp4',
          revision: 3,
          source: 'local',
        }),
      ).toBe(false);
      expect(
        await store.handleProjectEvent({
          kind: 'split',
          project_id: 'other.mp4',
          revision: 9,
          source: 'external',
        }),
      ).toBe(false);
      expect(open).not.toHaveBeenCalled();
      open.mockResolvedValue(state('gig.mp4', 4));
      timeline.mockResolvedValue(timelineOf(4));
      const reloaded = await store.handleProjectEvent({
        kind: 'split',
        project_id: 'gig.mp4',
        revision: 4,
        source: 'external',
      });
      expect(reloaded).toBe(true);
      expect(store.revision).toBe(4);
    });

    it('applies edit ops with If-Match, then reads the timeline for the new revision', async () => {
      const store = await openStore();
      applyOps.mockResolvedValue({ data: { revision: 4 } });
      timeline.mockResolvedValue(timelineOf(4));
      const ops = [{ op: 'trim_clip', clip: 'c0', out: 5 }];
      expect(await store.applyOps(ops)).toBe(true);
      expect(applyOps).toHaveBeenCalledWith({
        query: { path: 'gig.mp4' },
        body: { schema_version: 1, ops },
        headers: { 'If-Match': '"3"' },
      });
      expect(store.revision).toBe(4);
      applyOps.mockResolvedValue({ data: { revision: 5 } });
      timeline.mockResolvedValue(timelineOf(5));
      await store.applyOps([{ op: 'move_clip', clip: 'c0', to_index: 0 }]);
      expect(applyOps.mock.calls[1]?.[0].headers['If-Match']).toBe('"4"');
    });

    it('a stale revision on an ops edit reloads and shows the conflict, without re-applying', async () => {
      const store = await openStore();
      applyOps.mockRejectedValue(
        new ApiError({
          code: 'revision.conflict',
          message: 'stale',
          status: 409,
          currentRevision: 7,
        }),
      );
      open.mockResolvedValue(state('gig.mp4', 7));
      timeline.mockResolvedValue(timelineOf(7, ['gig.mp4', 'agent.mp4']));
      expect(await store.applyOps([{ op: 'trim_clip', clip: 'c0', out: 5 }])).toBe(false);
      expect(applyOps).toHaveBeenCalledTimes(1);
      expect(store.editError?.message).toMatch(/changed somewhere else/);
      expect(store.revision).toBe(7);
      expect(store.timeline?.tracks[0]?.clips).toHaveLength(2);
    });

    it('keeps the lost edit so the user can apply it again on the latest version', async () => {
      const store = await openStore();
      applyOps.mockRejectedValueOnce(
        new ApiError({
          code: 'revision.conflict',
          message: 'stale',
          status: 409,
          currentRevision: 7,
        }),
      );
      open.mockResolvedValue(state('gig.mp4', 7));
      timeline.mockResolvedValue(timelineOf(7));
      const ops = [{ op: 'trim_clip', clip: 'c0', out: 5 }];
      expect(await store.applyOps(ops)).toBe(false);
      expect(store.conflict).not.toBeNull();
      applyOps.mockResolvedValue({ data: { revision: 8 } });
      timeline.mockResolvedValue(timelineOf(8));
      expect(await store.conflict?.retry()).toBe(true);
      expect(applyOps.mock.calls[1]?.[0].headers['If-Match']).toBe('"7"');
      expect(applyOps.mock.calls[1]?.[0].body.ops).toEqual(ops);
      expect(store.conflict).toBeNull();
      expect(store.revision).toBe(8);
    });

    it('waits for its own edit in flight before judging an event, so it is not a foreign change', async () => {
      const store = await openStore();
      let release: (value: unknown) => void = () => undefined;
      applyOps.mockReturnValue(new Promise((resolve) => (release = resolve)));
      timeline.mockResolvedValue(timelineOf(4));
      const edit = store.applyOps([{ op: 'trim_clip', clip: 'c0', out: 5 }]);
      open.mockClear();
      const event = store.handleProjectEvent({
        kind: 'timeline',
        project_id: 'gig.mp4',
        revision: 4,
        source: 'local',
      });
      release({ data: { revision: 4 } });
      await edit;
      expect(await event).toBe(false);
      expect(open).not.toHaveBeenCalled();
    });

    it('does nothing without an open project', async () => {
      const store = new ProjectStore();
      expect(await store.addAsset('a_1')).toBe(false);
      expect(addClip).not.toHaveBeenCalled();
    });
  });
});
