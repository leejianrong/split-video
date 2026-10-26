import { describe, expect, it } from 'vitest';
import {
  MAX_PX_PER_SECOND,
  clampScroll,
  clampZoom,
  fitPxPerSecond,
  fitView,
  rulerTicks,
  sliderToZoom,
  tickLabel,
  tickStep,
  timeToX,
  visibleRange,
  visibleSlice,
  xToTime,
  zoomAt,
  zoomToSlider,
} from './scale';

const H3 = 3 * 3600;

describe('mapping', () => {
  it('time and x are inverse, and scrolling shifts x only', () => {
    const view = { pxPerSecond: 12.5, scrollLeft: 400 };
    expect(timeToX(100, view)).toBe(850);
    expect(xToTime(850, view)).toBe(100);
    expect(timeToX(32, view)).toBe(0);
  });

  it('fit puts the whole timeline in the width, and a short timeline is capped at the closest zoom', () => {
    expect(fitPxPerSecond(H3, 1080)).toBeCloseTo(0.1, 6);
    expect(fitView(H3, 1080)).toEqual({ pxPerSecond: fitPxPerSecond(H3, 1080), scrollLeft: 0 });
    expect(fitView(1, 1000).pxPerSecond).toBe(1000);
    expect(clampZoom(10_000, 60, 1000)).toBe(MAX_PX_PER_SECOND);
    expect(clampZoom(0.001, 60, 1000)).toBeCloseTo(1000 / 60, 6);
  });
});

describe('zoom', () => {
  it('keeps the time under the anchor at the same screen x, at several zoom levels', () => {
    let view = fitView(H3, 1080);
    const anchor = 640;
    const time = xToTime(anchor, view);
    for (const factor of [4, 4, 4, 2, 0.5, 8]) {
      view = zoomAt(view, factor, anchor, H3, 1080);
      expect(timeToX(time, view)).toBeCloseTo(anchor, 6);
    }
  });

  it('never zooms out past the fit or in past the closest zoom, and never scrolls past the ends', () => {
    const fit = fitPxPerSecond(H3, 1080);
    expect(zoomAt({ pxPerSecond: fit, scrollLeft: 0 }, 0.1, 500, H3, 1080).pxPerSecond).toBe(fit);
    expect(zoomAt({ pxPerSecond: 399, scrollLeft: 0 }, 10, 500, H3, 1080).pxPerSecond).toBe(
      MAX_PX_PER_SECOND,
    );
    expect(clampScroll(-5, H3, 2, 1080)).toBe(0);
    expect(clampScroll(1e9, H3, 2, 1080)).toBe(H3 * 2 - 1080);
    expect(clampScroll(10, 100, 1, 1080)).toBe(0); // content narrower than the view
  });

  it('the slider is logarithmic and round-trips', () => {
    expect(zoomToSlider(fitPxPerSecond(H3, 1080), H3, 1080)).toBeCloseTo(0, 6);
    expect(zoomToSlider(MAX_PX_PER_SECOND, H3, 1080)).toBeCloseTo(100, 6);
    for (const v of [0, 17, 50, 83, 100]) {
      expect(zoomToSlider(sliderToZoom(v, H3, 1080), H3, 1080)).toBeCloseTo(v, 6);
    }
  });
});

describe('virtualisation', () => {
  const clips = Array.from({ length: 1000 }, (_, i) => ({ start: i * 10, end: i * 10 + 9 }));

  it('returns the clips that touch the window, and only those', () => {
    expect(visibleSlice(clips, 0, 25)).toEqual([0, 3]);
    expect(visibleSlice(clips, 9.5, 10)).toEqual([1, 1]); // the gap between clips 0 and 1: nothing
    expect(visibleSlice(clips, 5000, 5030)).toEqual([500, 503]);
    expect(visibleSlice(clips, 20_000, 21_000)).toEqual([1000, 1000]);
    expect(visibleSlice([], 0, 10)).toEqual([0, 0]);
  });

  it('agrees with a linear scan for random windows (crossfade overlaps included)', () => {
    const overlapping = Array.from({ length: 300 }, (_, i) => ({ start: i * 8, end: i * 8 + 10 }));
    for (let n = 0; n < 200; n++) {
      const t0 = Math.random() * 2500;
      const t1 = t0 + Math.random() * 120;
      const [lo, hi] = visibleSlice(overlapping, t0, t1);
      const expected = overlapping.filter((c) => c.end > t0 && c.start < t1);
      expect(overlapping.slice(lo, hi)).toEqual(expected);
    }
  });

  it('a 3 h view keeps the visible window to a few clips, at every zoom', () => {
    for (const px of [0.1, 1, 10, 100]) {
      const view = { pxPerSecond: px, scrollLeft: 0 };
      const range = visibleRange(view, 1080, 200);
      const [lo, hi] = visibleSlice(clips, range.start, range.end);
      expect(hi - lo).toBeLessThanOrEqual(Math.ceil((1480 / px + 10) / 10) + 1);
    }
  });
});

describe('ruler', () => {
  it('picks the smallest spacing that keeps labels apart and labels it as a clock', () => {
    expect(tickStep(0.1).step).toBe(1800); // 96 px at 0.1 px/s is 960 s: the next spacing up
    expect(tickStep(10).step).toBe(10);
    expect(tickStep(400).step).toBe(0.5);
    expect(tickLabel(3725, 60)).toBe('1:02:05');
    expect(tickLabel(1.5, 0.5)).toBe('0:00:01.5');
    expect(tickLabel(1.25, 0.05)).toBe('0:00:01.25');
  });

  it('draws only the visible ticks, majors at the step, positioned by the same mapping', () => {
    for (const px of [0.1, 2, 80]) {
      const view = { pxPerSecond: px, scrollLeft: px * 5000 };
      const ticks = rulerTicks(view, 1000);
      expect(ticks.length).toBeLessThan(80);
      const { step } = tickStep(px);
      for (const t of ticks) {
        expect(t.x).toBeCloseTo(timeToX(t.time, view), 6);
        if (t.major) {
          expect(t.label).not.toBe('');
          expect(Math.abs(t.time / step - Math.round(t.time / step))).toBeLessThan(1e-6);
        } else {
          expect(t.label).toBe('');
        }
      }
      expect(ticks.some((t) => t.major)).toBe(true);
    }
  });
});
