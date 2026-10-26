import { describe, expect, it } from 'vitest';
import type { ClipOut, TrackOut } from '../api/client';
import {
  MIN_CLIP,
  dropOps,
  keyboardStep,
  placeOps,
  reorderIndex,
  reorderMarker,
  reorderOps,
  trimClip,
  trimLimits,
  trimOps,
} from './edits';
import type { Lane } from './lanes';

const clip = (id: string, start: number, dur: number, extra: Partial<ClipOut> = {}): ClipOut =>
  ({
    id,
    asset: 'a_1',
    source: 'gig.mp4',
    in_point: start,
    out_point: start + dur,
    start,
    end: start + dur,
    duration: dur,
    source_duration: 600,
    kind: 'media',
    label: '',
    text: '',
    ...extra,
  }) as ClipOut;

const lane = (
  clips: ClipOut[],
  magnetic: boolean,
  id = 't1',
  kind: Lane['kind'] = 'video',
): Lane => ({
  id,
  kind,
  name: id,
  track: {
    id,
    kind: kind === 'music' ? 'audio' : 'video',
    magnetic,
    clips,
    locked: false,
  } as TrackOut,
  clips,
  height: 40,
  top: 0,
  canHide: false,
  canMute: false,
  main: magnetic,
});

describe('trimming', () => {
  const c = clip('c', 100, 50); // source 100..150
  const open = { sourceDuration: 600, leftLimit: null, rightLimit: null };

  it('moves the end edge and produces a trim_clip with only the out point', () => {
    const t = trimClip(c, 'end', -10, open);
    expect(t).toEqual({ in: 100, out: 140, delta: -10 });
    expect(trimOps(c, 'end', t)).toEqual([{ op: 'trim_clip', clip: 'c', out: 140 }]);
  });

  it('moves the start edge and produces a trim_clip with only the in point', () => {
    const t = trimClip(c, 'start', 12.3456, open);
    expect(t).toEqual({ in: 112.346, out: 150, delta: 12.346 });
    expect(trimOps(c, 'start', t)).toEqual([{ op: 'trim_clip', clip: 'c', in: 112.346 }]);
  });

  it('cannot go past the source, below 0, or shorter than the shortest clip', () => {
    expect(trimClip(c, 'end', 1000, open).out).toBe(600);
    expect(trimClip(clip('z', 5, 20), 'start', -100, open).in).toBe(0);
    expect(trimClip(c, 'end', -1000, open).out).toBeCloseTo(100 + MIN_CLIP, 6);
    expect(trimClip(c, 'start', 1000, open).in).toBeCloseTo(150 - MIN_CLIP, 6);
  });

  it('a title has no source: its end can go as far as asked, its start only inward, never under the minimum', () => {
    const title = clip('t', 3, 2, {
      kind: 'text',
      in_point: 0,
      out_point: 2,
      source_duration: null,
    });
    expect(trimClip(title, 'end', 50, trimLimits(lane([title], false), title)).out).toBe(52);
    expect(trimClip(title, 'start', -5, trimLimits(lane([title], false), title)).in).toBe(0);
    expect(trimClip(title, 'start', 1, trimLimits(lane([title], false), title)).in).toBe(1);
  });

  it('on a free track stops at the neighbours, so the engine never sees an overlap', () => {
    const a = clip('a', 0, 10);
    const b = clip('b', 12, 5, { in_point: 30, out_point: 35 });
    const d = clip('d', 20, 5, { in_point: 50, out_point: 55 });
    const l = lane([a, b, d], false);
    const limits = trimLimits(l, b);
    expect(limits).toMatchObject({ leftLimit: 10, rightLimit: 20 });
    expect(trimClip(b, 'end', 100, limits).out).toBe(38); // 30 + (20 - 12) + 5 - 5 = until d starts
    expect(trimClip(b, 'start', -100, limits).in).toBe(28); // back to where a ends
    expect(trimLimits(lane([a, b, d], true), b)).toMatchObject({
      leftLimit: null,
      rightLimit: null,
    });
  });

  it('a trim that changes nothing sends nothing', () => {
    expect(trimOps(c, 'end', trimClip(c, 'end', 0, open))).toEqual([]);
  });
});

describe('reordering the main track', () => {
  const clips = [clip('a', 0, 10), clip('b', 10, 10), clip('c', 20, 10), clip('d', 30, 10)];

  it('the target index counts the clips whose middle is before the dragged clip middle', () => {
    expect(reorderIndex(clips, 0, 0)).toBe(0);
    expect(reorderIndex(clips, 0, 12)).toBe(1); // a's middle is 5 -> 17: past b (15), before c (25)
    expect(reorderIndex(clips, 0, 26)).toBe(2);
    expect(reorderIndex(clips, 0, 31)).toBe(3);
    expect(reorderIndex(clips, 3, -100)).toBe(0);
    expect(reorderIndex(clips, 1, 4)).toBe(1); // a small nudge stays put
  });

  it('commits one move_clip, or nothing when the clip stays', () => {
    expect(reorderOps(clips, 0, 2)).toEqual([{ op: 'move_clip', clip: 'a', to_index: 2 }]);
    expect(reorderOps(clips, 2, 2)).toEqual([]);
  });

  it('the drop marker sits between the neighbours the clip would land between', () => {
    expect(reorderMarker(clips, 0, 0)).toBe(10); // before b: where b starts once a is lifted out
    expect(reorderMarker(clips, 0, 2)).toBe(30); // after c (at 20..30)
    expect(reorderMarker(clips, 3, 3)).toBe(30);
  });
});

describe('placing on a free track', () => {
  const c = clip('t', 20, 5);
  it('moves in time, clamped at 0, rounded to a millisecond', () => {
    expect(placeOps(c, 't1', 4.0004, 't1')).toEqual([{ op: 'place_clip', clip: 't', start: 24 }]);
    expect(placeOps(c, 't1', -50, 't1')).toEqual([{ op: 'place_clip', clip: 't', start: 0 }]);
  });
  it('names the lane only when it changed, and sends nothing for no move', () => {
    expect(placeOps(c, 't1', 1, 't2')).toEqual([
      { op: 'place_clip', clip: 't', start: 21, track: 't2' },
    ]);
    expect(placeOps(c, 't1', 0, 't1')).toEqual([]);
  });
});

describe('dropping a media card', () => {
  const mainClips = [clip('a', 0, 10), clip('b', 10, 10)];
  const main = lane(mainClips, true, 'main');
  const overlay = lane([clip('o', 5, 3)], false, 'ov');
  const music = lane([clip('m', 0, 4)], false, 'mu', 'music');
  const lanes = [overlay, main, music];
  const video = { id: 'a_v', has_video: true };
  const audio = { id: 'a_a', has_video: false };

  it('on the main lane inserts at the drop position', () => {
    expect(dropOps(video, main, 3, lanes, mainClips)).toEqual([
      { op: 'add_clip', asset: 'a_v', index: 0 },
    ]);
    expect(dropOps(video, main, 16, lanes, mainClips)).toEqual([
      { op: 'add_clip', asset: 'a_v', index: 2 },
    ]);
    expect(dropOps(video, null, 12, lanes, mainClips)).toEqual([
      { op: 'add_clip', asset: 'a_v', index: 1 },
    ]);
  });

  it('on another lane places at the drop time', () => {
    expect(dropOps(video, overlay, 7.1234, lanes, mainClips)).toEqual([
      { op: 'add_clip', asset: 'a_v', track: 'ov', start: 7.123 },
    ]);
  });

  it('audio goes to an audio lane, or to a new audio track when there is none', () => {
    expect(dropOps(audio, main, 2, lanes, mainClips)).toEqual([
      { op: 'add_clip', asset: 'a_a', track: 'mu', start: 2 },
    ]);
    const ops = dropOps(audio, main, 2, [overlay, main], mainClips);
    expect(ops[0]).toMatchObject({ op: 'add_track', kind: 'audio' });
    expect(ops[1]).toMatchObject({ op: 'add_clip', asset: 'a_a', start: 2, track: ops[0]?.id });
    expect(String(ops[0]?.id)).toMatch(/^[A-Za-z0-9_.-]{1,64}$/);
  });
});

describe('keyboard steps', () => {
  it('is a second when zoomed far out and a tenth when close', () => {
    expect(keyboardStep(0.1)).toBe(1);
    expect(keyboardStep(4.9)).toBe(1);
    expect(keyboardStep(5)).toBe(0.1);
  });
});
