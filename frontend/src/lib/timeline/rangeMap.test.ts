import { describe, expect, it } from 'vitest';
import { sourceToTimeline } from './rangeMap';

const clip = (asset: string, in_point: number, out_point: number, start: number) => ({
  asset,
  in_point,
  out_point,
  start,
});

describe('sourceToTimeline', () => {
  it('is the identity for a recording that is one clip', () => {
    expect(sourceToTimeline([clip('a', 0, 100, 0)], 'a', 10, 40)).toEqual([{ start: 10, end: 40 }]);
  });

  it('follows a clip that was cut and moved', () => {
    const clips = [clip('a', 50, 80, 0), clip('a', 0, 30, 30)];
    expect(sourceToTimeline(clips, 'a', 10, 20)).toEqual([{ start: 40, end: 50 }]);
  });

  it('splits a range across two clips and drops the part that is cut away', () => {
    const clips = [clip('a', 0, 20, 0), clip('a', 30, 60, 20)];
    expect(sourceToTimeline(clips, 'a', 15, 40)).toEqual([
      { start: 15, end: 20 },
      { start: 20, end: 30 },
    ]);
  });

  it('ignores other assets and ranges that are not on the timeline', () => {
    expect(sourceToTimeline([clip('b', 0, 100, 0)], 'a', 10, 40)).toEqual([]);
    expect(sourceToTimeline([clip('a', 0, 10, 0)], 'a', 20, 40)).toEqual([]);
  });
});
