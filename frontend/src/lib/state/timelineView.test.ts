import { beforeEach, describe, expect, it } from 'vitest';
import { timeToX } from '../timeline/scale';
import { playhead } from './playhead.svelte';
import { TimelineViewStore } from './timelineView.svelte';
import { ClipSelectionStore } from './clipSelection.svelte';

beforeEach(() => playhead.reset());

describe('TimelineViewStore', () => {
  it('starts fitted: the whole timeline in the width, and refits when the window or length changes', () => {
    const view = new TimelineViewStore();
    view.setSize(1000, 10_000);
    expect(view.pxPerSecond).toBeCloseTo(0.1, 8);
    expect(view.fitted).toBe(true);
    view.setSize(1200, 10_000);
    expect(view.pxPerSecond).toBeCloseTo(0.12, 8);
    view.setSize(1200, 6000);
    expect(view.pxPerSecond).toBeCloseTo(0.2, 8);
    expect(playhead.duration).toBe(6000);
  });

  it('zooms around the playhead when it is on screen, else around the middle, and stops being fitted', () => {
    const view = new TimelineViewStore();
    view.setSize(1000, 10_000);
    playhead.seek(2500);
    const x = timeToX(2500, view.view);
    view.zoomBy(8);
    expect(view.fitted).toBe(false);
    expect(timeToX(2500, view.view)).toBeCloseTo(x, 6);
    const mid = (view.scrollLeft + 500) / view.pxPerSecond;
    view.zoomBy(2, 500);
    expect((view.scrollLeft + 500) / view.pxPerSecond).toBeCloseTo(mid, 6);
  });

  it('zooming back out to the fit makes it fitted again; fit() does too; scroll stays in range', () => {
    const view = new TimelineViewStore();
    view.setSize(1000, 10_000);
    view.zoomBy(10, 0);
    view.zoomBy(0.1, 0);
    expect(view.fitted).toBe(true);
    view.zoomBy(50, 0);
    view.scrollTo(1e12);
    expect(view.scrollLeft).toBe(10_000 * view.pxPerSecond - 1000);
    view.scrollBy(-1e12);
    expect(view.scrollLeft).toBe(0);
    view.fit();
    expect(view.fitted).toBe(true);
    expect(view.pxPerSecond).toBeCloseTo(0.1, 8);
    view.zoomTo(5);
    expect(view.pxPerSecond).toBeCloseTo(5, 6);
  });

  it('keeps the zoom (clamped) when the window grows while zoomed in', () => {
    const view = new TimelineViewStore();
    view.setSize(1000, 10_000);
    view.zoomBy(100, 0);
    const px = view.pxPerSecond;
    view.setSize(1400, 10_000);
    expect(view.pxPerSecond).toBe(px);
    expect(view.fitted).toBe(false);
  });
});

describe('ClipSelectionStore', () => {
  it('a plain click replaces, a shift-click toggles, prune drops what is gone, clear empties', () => {
    const sel = new ClipSelectionStore();
    sel.select('a');
    sel.select('b', true);
    expect(sel.ids).toEqual(['a', 'b']);
    expect(sel.primary).toBe('b');
    sel.select('a', true);
    expect(sel.ids).toEqual(['b']);
    sel.select('c');
    expect(sel.ids).toEqual(['c']);
    sel.select('d', true);
    sel.prune(new Set(['c']));
    expect(sel.ids).toEqual(['c']);
    expect(sel.has('c')).toBe(true);
    sel.clear();
    expect(sel.ids).toEqual([]);
    expect(sel.primary).toBeNull();
  });
});
