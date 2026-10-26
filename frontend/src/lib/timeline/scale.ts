/**
 * Time <-> pixel mapping for the timeline (pure, no DOM). A view is a zoom (`pxPerSecond`) and a scroll offset in
 * pixels from the start of the timeline. Everything the lanes draw is `time * pxPerSecond - scrollLeft`, so a clip's
 * x, the ruler ticks and the playhead agree by construction, and only the visible slice is ever turned into elements.
 */
import { formatClock } from '../format';

export interface View {
  pxPerSecond: number;
  /** Pixels between the start of the timeline and the left edge of the lanes. */
  scrollLeft: number;
}

/** The closest zoom: 400 px per second (a frame is 13 px wide at 30 fps). */
export const MAX_PX_PER_SECOND = 400;

export function timeToX(time: number, view: View): number {
  return time * view.pxPerSecond - view.scrollLeft;
}

export function xToTime(x: number, view: View): number {
  return (x + view.scrollLeft) / view.pxPerSecond;
}

/** The zoom at which the whole timeline fits `width` px. */
export function fitPxPerSecond(duration: number, width: number): number {
  return Math.max(width, 1) / Math.max(duration, 1);
}

/** Zoom limits: the whole timeline in view, up to `MAX_PX_PER_SECOND` (a very short timeline may start closer). */
export function zoomLimits(duration: number, width: number): { min: number; max: number } {
  const fit = fitPxPerSecond(duration, width);
  return { min: Math.min(fit, MAX_PX_PER_SECOND), max: Math.max(fit, MAX_PX_PER_SECOND) };
}

export function clampZoom(pxPerSecond: number, duration: number, width: number): number {
  const { min, max } = zoomLimits(duration, width);
  return Math.min(max, Math.max(min, pxPerSecond));
}

export function clampScroll(
  scrollLeft: number,
  duration: number,
  pxPerSecond: number,
  width: number,
): number {
  return Math.min(Math.max(0, duration * pxPerSecond - width), Math.max(0, scrollLeft));
}

/**
 * Zoom by `factor` keeping the time under screen x `anchorX` where it is (the playhead, or the pointer for the
 * wheel). The result is clamped to the zoom limits and the scroll range.
 */
export function zoomAt(
  view: View,
  factor: number,
  anchorX: number,
  duration: number,
  width: number,
): View {
  const pxPerSecond = clampZoom(view.pxPerSecond * factor, duration, width);
  const anchorTime = xToTime(anchorX, view);
  return {
    pxPerSecond,
    scrollLeft: clampScroll(anchorTime * pxPerSecond - anchorX, duration, pxPerSecond, width),
  };
}

/** The view with the whole timeline showing. */
export function fitView(duration: number, width: number): View {
  return {
    pxPerSecond: clampZoom(fitPxPerSecond(duration, width), duration, width),
    scrollLeft: 0,
  };
}

/** The time range on screen, widened by `overscanPx` each side so elements just off screen stay mounted. */
export function visibleRange(
  view: View,
  width: number,
  overscanPx = 0,
): { start: number; end: number } {
  return {
    start: Math.max(0, xToTime(-overscanPx, view)),
    end: xToTime(width + overscanPx, view),
  };
}

interface Span {
  start: number;
  end: number;
}

/**
 * The index range `[lo, hi)` of the items that touch `[t0, t1)`, for items sorted by start whose ends never
 * decrease (a magnetic track, or a free track that forbids overlaps): two binary searches, so a track with
 * thousands of clips costs a handful of comparisons per frame.
 */
export function visibleSlice(items: readonly Span[], t0: number, t1: number): [number, number] {
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((items[mid] as Span).end <= t0) lo = mid + 1;
    else hi = mid;
  }
  const first = lo;
  hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((items[mid] as Span).start < t1) lo = mid + 1;
    else hi = mid;
  }
  return [first, Math.max(first, lo)];
}

// ---------------------------------------------------------------------------------------------------------
// Ruler

/** Label spacings in seconds, and how many minor ticks divide each. */
const TICK_STEPS: readonly (readonly [number, number])[] = [
  [0.05, 5],
  [0.1, 5],
  [0.2, 4],
  [0.5, 5],
  [1, 5],
  [2, 4],
  [5, 5],
  [10, 5],
  [15, 5],
  [30, 6],
  [60, 6],
  [120, 4],
  [300, 5],
  [600, 5],
  [900, 5],
  [1800, 6],
  [3600, 6],
  [7200, 4],
  [14400, 4],
];

/** The smallest label spacing (seconds) that keeps labels at least `minPx` apart at this zoom. */
export function tickStep(
  pxPerSecond: number,
  minPx = 96,
): { step: number; minorDivisions: number } {
  const found =
    TICK_STEPS.find(([step]) => step * pxPerSecond >= minPx) ??
    (TICK_STEPS[TICK_STEPS.length - 1] as readonly [number, number]);
  return { step: found[0], minorDivisions: found[1] };
}

export interface Tick {
  time: number;
  x: number;
  major: boolean;
  /** Only major ticks have a label. */
  label: string;
}

/** "0:41:12" and, below a second of spacing, "0:41:12.5". */
export function tickLabel(time: number, step: number): string {
  if (step >= 1) return formatClock(time);
  const decimals = step < 0.1 ? 2 : 1;
  const fraction = (time - Math.floor(time)).toFixed(decimals).slice(1);
  return formatClock(time) + fraction;
}

/** The ticks on screen: majors with labels at the adaptive spacing, minors between them when they fit. */
export function rulerTicks(view: View, width: number, minLabelPx = 96): Tick[] {
  const { step, minorDivisions } = tickStep(view.pxPerSecond, minLabelPx);
  const minorStep = step / minorDivisions;
  const showMinor = minorStep * view.pxPerSecond >= 10;
  const range = visibleRange(view, width, 2);
  const unit = showMinor ? minorStep : step;
  const first = Math.floor(range.start / unit);
  const last = Math.ceil(range.end / unit);
  const out: Tick[] = [];
  for (let i = first; i <= last; i++) {
    const time = Math.round(i * unit * 1000) / 1000;
    const major = Math.abs(time / step - Math.round(time / step)) < 1e-6;
    out.push({ time, x: timeToX(time, view), major, label: major ? tickLabel(time, step) : '' });
  }
  return out;
}

// ---------------------------------------------------------------------------------------------------------
// Zoom slider: a logarithmic scale between the limits, 0 to 100.

export function zoomToSlider(pxPerSecond: number, duration: number, width: number): number {
  const { min, max } = zoomLimits(duration, width);
  if (max <= min) return 0;
  return (Math.log(pxPerSecond / min) / Math.log(max / min)) * 100;
}

export function sliderToZoom(value: number, duration: number, width: number): number {
  const { min, max } = zoomLimits(duration, width);
  return min * (max / min) ** (Math.min(100, Math.max(0, value)) / 100);
}
