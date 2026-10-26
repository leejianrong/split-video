import { beforeEach, describe, expect, it, vi } from 'vitest';

const suggest = vi.fn();
const reviews = vi.fn();
const putReview = vi.fn();
const make = vi.fn();
const open = vi.fn();
const timeline = vi.fn();
vi.mock('../api/generated/sdk.gen', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/generated/sdk.gen')>()),
  getHighlightsApiV1AssetsAssetIdHighlightsGet: (...a: unknown[]) => suggest(...a),
  getHighlightReviewsApiV1AssetsAssetIdHighlightReviewsGet: (...a: unknown[]) => reviews(...a),
  putHighlightReviewApiV1AssetsAssetIdHighlightReviewsKeyPut: (...a: unknown[]) => putReview(...a),
  makeHighlightClipsApiV1ProjectHighlightsPost: (...a: unknown[]) => make(...a),
  openFileApiV1OpenPost: (...a: unknown[]) => open(...a),
  getVideoTimelineApiV1ProjectTimelineGet: (...a: unknown[]) => timeline(...a),
}));

import { ApiError, type AssetOut, type JobOut } from '../api/client';
import { ClipSelectionStore } from './clipSelection.svelte';
import { HighlightsStore, keyOf, toSuggestions } from './highlights.svelte';
import { JobsStore } from './jobs.svelte';
import { PlayheadStore } from './playhead.svelte';
import { ProjectStore } from './project.svelte';
import { RangeSelectionStore } from './rangeSelection.svelte';

const asset = { id: 'a_1', path: 'gig.mp4', duration: 600, present: true } as AssetOut;

const cand = (rank: number, start: number, end: number, extra: Record<string, unknown> = {}) => ({
  rank,
  start,
  end,
  duration: end - start,
  score: 0.92 - rank / 10,
  reasons: [{ note: 'loud, crowd cheers' }, { note: 'speech 3.2 words/s' }, { note: 'third' }],
  ...extra,
});

const doc = (revision: number, entries: Record<string, unknown> = {}) => ({
  data: { version: 1, revision, reviews: entries },
});

async function setup() {
  const jobs = new JobsStore(() => () => undefined);
  const project = new ProjectStore();
  open.mockResolvedValue({
    data: {
      filename: 'gig.mp4',
      duration: 600,
      params: {},
      silences: [],
      segments: [],
      revision: 5,
    },
  });
  timeline.mockResolvedValue({ data: { name: 'gig.mp4', revision: 5, duration: 600, tracks: [] } });
  await project.open('gig.mp4');
  const playhead = new PlayheadStore();
  playhead.duration = 600;
  const range = new RangeSelectionStore();
  const clips = new ClipSelectionStore();
  const store = new HighlightsStore(jobs, project, playhead, range, clips);
  return { jobs, project, playhead, range, clips, store };
}

beforeEach(() => {
  for (const m of [suggest, reviews, putReview, make, open, timeline]) m.mockReset();
  suggest.mockResolvedValue({
    data: {
      notes: ['2 candidates'],
      candidates: [cand(1, 250, 298, { title_hint: 'The opener' }), cand(2, 3151, 3194.5)],
    },
  });
  reviews.mockResolvedValue(doc(0));
});

describe('keyOf and toSuggestions', () => {
  it('makes the engine key of a window to the millisecond', () => {
    expect(keyOf(250, 298.5)).toBe('h_250000_298500');
    expect(keyOf(3151.001, 3194.5)).toBe('h_3151001_3194500');
  });

  it('merges candidates with reviews, adjusted edges first', () => {
    const [first, second, third] = toSuggestions(
      [cand(1, 250, 298, { title_hint: 'The opener' }), cand(2, 10, 40), cand(3, 60, 90)],
      {
        h_250000_298000: {
          state: 'accepted',
          start: 250,
          end: 298,
          adjusted: { start: 252, end: 290 },
        },
        h_10000_40000: { state: 'dismissed', start: 10, end: 40 },
      },
    );
    expect(first).toMatchObject({
      title: 'The opener',
      score: 82,
      state: 'accepted',
      from: 252,
      to: 290,
      adjusted: true,
      reasons: ['loud, crowd cheers', 'speech 3.2 words/s', 'third'],
    });
    expect(second).toMatchObject({ title: 'Highlight 2', state: 'dismissed', from: 10, to: 40 });
    expect(third).toMatchObject({ state: 'pending', from: 60, to: 90, adjusted: false });
  });
});

describe('HighlightsStore.load', () => {
  it('reads the suggestions and the reviews of the asset', async () => {
    const { store } = await setup();
    reviews.mockResolvedValue(
      doc(2, { h_250000_298000: { state: 'accepted', start: 250, end: 298 } }),
    );
    await store.load(asset);
    expect(store.status).toBe('ready');
    expect(store.suggestions.map((s) => [s.title, s.state])).toEqual([
      ['The opener', 'accepted'],
      ['Highlight 2', 'pending'],
    ]);
    expect(suggest).toHaveBeenCalledWith({ path: { asset_id: 'a_1' }, query: { count: 12 } });
  });

  it('asks for a preset and a keyword', async () => {
    const { store } = await setup();
    await store.load(asset);
    await store.setPreset('gig');
    await store.setKeyword('  finally ');
    expect(suggest).toHaveBeenLastCalledWith({
      path: { asset_id: 'a_1' },
      query: { count: 12, preset: 'gig', keywords: 'finally' },
    });
    await store.setPreset(null);
    expect(suggest.mock.lastCall?.[0].query).toEqual({ count: 12, keywords: 'finally' });
  });

  it('is the empty state, not an error, when no analysis is stored yet (409)', async () => {
    const { store } = await setup();
    suggest.mockRejectedValue(
      new ApiError({ code: 'analysis.missing', message: 'nothing stored', status: 409 }),
    );
    await store.load(asset);
    expect(store.status).toBe('missing');
    expect(store.error).toBeNull();
  });

  it('keeps the catalogue fix of a real failure', async () => {
    const { store } = await setup();
    suggest.mockRejectedValue(
      new ApiError({
        code: 'asset.unknown',
        message: 'Unknown asset.',
        fix: 'Import it again.',
        status: 404,
      }),
    );
    await store.load(asset);
    expect(store.status).toBe('error');
    expect(store.error).toMatchObject({ code: 'asset.unknown', fix: 'Import it again.' });
  });

  it('reads again when Find highlights finishes for the shown asset', async () => {
    const { store, jobs } = await setup();
    await store.load(asset);
    suggest.mockClear();
    jobs.ingest({ id: 'j9', kind: 'analysis', state: 'running', label: 'gig.mp4' } as JobOut);
    jobs.ingest({ id: 'j9', kind: 'analysis', state: 'done', label: 'gig.mp4' } as JobOut);
    await vi.waitFor(() => expect(suggest).toHaveBeenCalledTimes(1));
    jobs.ingest({ id: 'j10', kind: 'analysis', state: 'running', label: 'other.mp4' } as JobOut);
    jobs.ingest({ id: 'j10', kind: 'analysis', state: 'done', label: 'other.mp4' } as JobOut);
    expect(suggest).toHaveBeenCalledTimes(1);
  });
});

describe('review', () => {
  it('writes the sidecar with the reviews revision and shows the new state', async () => {
    const { store } = await setup();
    reviews.mockResolvedValue(doc(3));
    await store.load(asset);
    putReview.mockResolvedValue(
      doc(4, { h_250000_298000: { state: 'dismissed', start: 250, end: 298 } }),
    );
    expect(await store.review('h_250000_298000', 'dismissed')).toBe(true);
    expect(putReview).toHaveBeenCalledWith({
      path: { asset_id: 'a_1', key: 'h_250000_298000' },
      body: {
        state: 'dismissed',
        start: 250,
        end: 298,
        adjusted_start: 250,
        adjusted_end: 298,
      },
      headers: { 'If-Match': '"3"' },
    });
    expect(store.suggestions[0]?.state).toBe('dismissed');
    expect(store.visible.map((s) => s.title)).toEqual(['Highlight 2']);
    expect(store.dismissedCount).toBe(1);
    store.showDismissed = true;
    expect(store.visible).toHaveLength(2);
  });

  it('undoes a dismissal by clearing the review (null state)', async () => {
    const { store } = await setup();
    reviews.mockResolvedValue(
      doc(1, { h_250000_298000: { state: 'dismissed', start: 250, end: 298 } }),
    );
    await store.load(asset);
    putReview.mockResolvedValue(doc(2));
    await store.review('h_250000_298000', 'pending');
    expect(putReview.mock.calls[0]?.[0].body.state).toBeNull();
    expect(store.suggestions[0]?.state).toBe('pending');
  });

  it('keeps adjusted edges, and resets them by sending the scored window', async () => {
    const { store } = await setup();
    await store.load(asset);
    putReview.mockResolvedValue(
      doc(1, {
        h_250000_298000: {
          state: 'pending',
          start: 250,
          end: 298,
          adjusted: { start: 255, end: 290 },
        },
      }),
    );
    await store.adjust('h_250000_298000', 255, 290);
    expect(putReview.mock.calls[0]?.[0].body).toMatchObject({
      state: 'pending',
      adjusted_start: 255,
      adjusted_end: 290,
    });
    expect(store.suggestions[0]).toMatchObject({ from: 255, to: 290, adjusted: true });
    putReview.mockResolvedValue(doc(2));
    await store.adjust('h_250000_298000', 250, 298);
    expect(putReview.mock.calls[1]?.[0].body.state).toBeNull();
    expect(store.suggestions[0]).toMatchObject({ from: 250, to: 298, adjusted: false });
  });

  it('retries once on another writer revision, and reports a refusal with its fix', async () => {
    const { store } = await setup();
    await store.load(asset);
    putReview
      .mockRejectedValueOnce(
        new ApiError({
          code: 'revision.conflict',
          message: 'stale',
          status: 409,
          currentRevision: 7,
        }),
      )
      .mockResolvedValueOnce(
        doc(8, { h_250000_298000: { state: 'accepted', start: 250, end: 298 } }),
      );
    expect(await store.review('h_250000_298000', 'accepted')).toBe(true);
    expect(putReview.mock.calls[1]?.[0].headers).toEqual({ 'If-Match': '"7"' });
    putReview.mockRejectedValue(
      new ApiError({
        code: 'highlights.invalid',
        message: 'end is past the end of the recording',
        fix: 'Use an earlier end.',
        status: 422,
      }),
    );
    expect(await store.review('h_3151000_3194500', 'accepted')).toBe(false);
    expect(store.actionError).toMatchObject({ fix: 'Use an earlier end.' });
  });
});

describe('select and hover', () => {
  it('moves the playhead through the clips and shows the range; hover shows, leaving restores', async () => {
    const { store, playhead, range } = await setup();
    await store.load(asset);
    // the recording was cut: source 200..400 now starts at 30 on the timeline
    store.select('h_250000_298000', [{ asset: 'a_1', in_point: 200, out_point: 400, start: 30 }]);
    expect(playhead.time).toBe(80);
    expect(range.range).toEqual({ assetId: 'a_1', start: 250, end: 298 });
    store.hover('h_3151000_3194500');
    expect(range.range).toMatchObject({ start: 3151 });
    store.hover(null);
    expect(range.range).toMatchObject({ start: 250 });
    store.select(null);
    expect(range.range).toBeNull();
  });
});

describe('make', () => {
  const made = {
    data: {
      revision: 6,
      made: [
        {
          key: 'h_250000_298000',
          clip: 'hlabc1',
          label: 'Short: The opener',
          start: 250,
          end: 298,
        },
      ],
      reviews: {
        version: 1,
        revision: 1,
        reviews: { h_250000_298000: { state: 'accepted', start: 250, end: 298 } },
      },
    },
  };

  it('sends the cards with the project revision, accepts them, selects the clip and moves the playhead', async () => {
    const { store, project, playhead, clips } = await setup();
    await store.load(asset);
    make.mockResolvedValue(made);
    timeline.mockResolvedValue({
      data: {
        name: 'gig.mp4',
        revision: 6,
        duration: 648,
        tracks: [
          {
            id: 't1',
            clips: [{ id: 'hlabc1', start: 600, asset: 'a_1', in_point: 250, out_point: 298 }],
          },
        ],
      },
    });
    const result = await store.make(['h_250000_298000'], 'short');
    expect(result).toHaveLength(1);
    expect(make).toHaveBeenCalledWith({
      query: { path: 'gig.mp4' },
      body: {
        asset: 'a_1',
        kind: 'short',
        items: [{ start: 250, end: 298, title: 'The opener', scored: { start: 250, end: 298 } }],
      },
      headers: { 'If-Match': '"5"' },
    });
    expect(project.revision).toBe(6);
    expect(store.suggestions[0]?.state).toBe('accepted');
    expect(clips.ids).toEqual(['hlabc1']);
    expect(playhead.time).toBe(600);
  });

  it('leaves a title-less suggestion to the engine, and sends adjusted edges with the scored window', async () => {
    const { store } = await setup();
    reviews.mockResolvedValue(
      doc(1, {
        h_3151000_3194500: {
          state: 'pending',
          start: 3151,
          end: 3194.5,
          adjusted: { start: 3155, end: 3190 },
        },
      }),
    );
    await store.load(asset);
    make.mockResolvedValue({ data: { ...made.data, made: [] } });
    timeline.mockResolvedValue({
      data: { name: 'gig.mp4', revision: 6, duration: 600, tracks: [] },
    });
    await store.make(['h_3151000_3194500'], 'clip');
    expect(make.mock.calls[0]?.[0].body).toMatchObject({
      kind: 'clip',
      items: [{ start: 3155, end: 3190, title: '', scored: { start: 3151, end: 3194.5 } }],
    });
  });

  it('on a stale project revision reloads the project and reports it; nothing is accepted', async () => {
    const { store, project } = await setup();
    await store.load(asset);
    make.mockRejectedValue(
      new ApiError({
        code: 'revision.conflict',
        message: 'stale',
        status: 409,
        currentRevision: 9,
      }),
    );
    expect(await store.make(['h_250000_298000'], 'short')).toBeNull();
    expect(project.editError?.message).toContain('changed somewhere else');
    expect(store.actionError?.message).toContain('changed somewhere else');
    expect(store.suggestions[0]?.state).toBe('pending');
    expect(store.busy).toBe(false);
  });

  it('shows the engine refusal and its fix (a locked track)', async () => {
    const { store } = await setup();
    await store.load(asset);
    make.mockRejectedValue(
      new ApiError({
        code: 'ops.invalid',
        message: 'track is locked',
        fix: 'Fix: unlock the lane.',
        status: 422,
      }),
    );
    expect(await store.make(['h_250000_298000'], 'short')).toBeNull();
    expect(store.actionError).toMatchObject({
      message: 'track is locked',
      fix: 'Fix: unlock the lane.',
    });
  });

  it('counts what "Make a short from every highlight" would make: the ones nobody acted on', async () => {
    const { store } = await setup();
    reviews.mockResolvedValue(
      doc(1, {
        h_250000_298000: { state: 'accepted', start: 250, end: 298 },
      }),
    );
    await store.load(asset);
    expect(store.pending.map((s) => s.key)).toEqual(['h_3151000_3194500']);
  });
});
