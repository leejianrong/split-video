import { describe, expect, it } from 'vitest';
import type { ClipOut, ProjectOut } from '../api/client';
import {
  buildRequest,
  DEFAULTS,
  defaultFolder,
  estimateSeconds,
  formatEstimate,
  plannedFiles,
  type ExportSettings,
} from './plan';

const clip = (over: Partial<ClipOut> & { id: string }): ClipOut =>
  ({
    asset: 'a_1',
    source: 'gig.mp4',
    in_point: 0,
    out_point: 60,
    kind: 'media',
    label: '',
    included: true,
    ...over,
  }) as ClipOut;

const timeline = (clips: ClipOut[]): ProjectOut =>
  ({
    name: 'gig.mp4',
    duration: 300,
    tracks: [{ id: 't1', kind: 'video', magnetic: true, clips }],
  }) as ProjectOut;

const tl = timeline([
  clip({ id: 'c1', label: 'Song 1' }),
  clip({ id: 'c2', label: 'Song 2', included: false }),
  clip({ id: 'c3', label: 'Song 3', in_point: 100, out_point: 220 }),
  clip({ id: 'c4', label: 'Short: Big chorus', in_point: 30, out_point: 50 }),
  clip({ id: 'c5', label: 'Highlight 2', in_point: 70, out_point: 90 }),
]);

const settings = (over: Partial<ExportSettings> = {}): ExportSettings => ({ ...DEFAULTS, ...over });

describe('plannedFiles', () => {
  it('lists the included segments, not the highlight clips', () => {
    const files = plannedFiles(tl, 'segments');
    expect(files.map((f) => f.number)).toEqual([1, 3]);
    expect(files.map((f) => f.seconds)).toEqual([60, 120]);
  });

  it('lists the highlight clips (shorts and highlights) for shorts, numbered on the main track', () => {
    expect(plannedFiles(tl, 'shorts').map((f) => [f.number, f.label])).toEqual([
      [4, 'Short: Big chorus'],
      [5, 'Highlight 2'],
    ]);
  });

  it('is one file for the whole timeline, and none for an empty one', () => {
    expect(plannedFiles(tl, 'timeline')).toHaveLength(1);
    expect(plannedFiles(timeline([]), 'timeline')).toEqual([]);
    expect(plannedFiles(null, 'segments')).toEqual([]);
  });
});

describe('buildRequest', () => {
  const files = plannedFiles(tl, 'segments');

  it('sends the numbers of the chosen segments with the preset and fit', () => {
    const req = buildRequest('shows/gig.mp4', settings({ fit: 'crop' }), files);
    expect(req).toMatchObject({
      mode: 'segments',
      video: 'shows/gig.mp4',
      only: [1, 3],
      preset: 'youtube-1080p',
      fit: 'crop',
      output_dir: 'shows/gig_export',
      overwrite: false,
    });
    expect(req.captions).toBeUndefined();
  });

  it('is a stream copy for the original quality (no preset, no captions)', () => {
    const req = buildRequest('gig.mp4', settings({ preset: 'original', captions: true }), files);
    expect(req.preset).toBeUndefined();
    expect(req.captions).toBeUndefined();
  });

  it('makes shorts vertical, cropped and captioned whatever the format says', () => {
    const shorts = plannedFiles(tl, 'shorts');
    const req = buildRequest(
      'gig.mp4',
      settings({ mode: 'shorts', preset: 'draft-720', fit: 'letterbox', captions: true }),
      shorts,
    );
    expect(req).toMatchObject({
      mode: 'segments',
      only: [4, 5],
      preset: 'vertical-1080',
      fit: 'crop',
      captions: 'shorts',
    });
  });

  it('exports the whole timeline in one call and honours the chosen folder and `only` override', () => {
    expect(
      buildRequest('gig.mp4', settings({ mode: 'timeline', folder: ' out ' }), []),
    ).toMatchObject({
      mode: 'timeline',
      output_dir: 'out',
    });
    expect(buildRequest('gig.mp4', settings(), files, [3]).only).toEqual([3]);
  });
});

describe('defaultFolder', () => {
  it('is <recording>_export beside the recording', () => {
    expect(defaultFolder('gig.mp4')).toBe('gig_export');
    expect(defaultFolder('a/b/my gig.final.mov')).toBe('a/b/my gig.final_export');
  });
});

describe('estimate', () => {
  const files = plannedFiles(tl, 'segments');
  it('is much shorter for a stream copy than for a render', () => {
    const copy = estimateSeconds(files, settings({ preset: 'original' }));
    const render = estimateSeconds(files, settings());
    expect(copy).toBeLessThan(render);
  });
  it('words it for people', () => {
    expect(formatEstimate(3)).toBe('under a minute');
    expect(formatEstimate(240)).toBe('about 4 min');
    expect(formatEstimate(3 * 3600 + 600)).toBe('about 3 h 10 min');
  });
});
