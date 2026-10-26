import { clampScroll, clampZoom, fitView, timeToX, zoomAt, type View } from '../timeline/scale';
import { playhead } from './playhead.svelte';

/**
 * Zoom and scroll of the timeline. `fitted` is the default and means "the whole timeline shows": it stays on until
 * the user zooms in, and while it is on a new project length or window width refits. All maths is in
 * `timeline/scale.ts`; this holds the numbers.
 */
export class TimelineViewStore {
  pxPerSecond = $state(1);
  scrollLeft = $state(0);
  /** Width of the lanes area in px (set by the timeline from its element). */
  width = $state(0);
  duration = $state(0);
  fitted = $state(true);

  get view(): View {
    return { pxPerSecond: this.pxPerSecond, scrollLeft: this.scrollLeft };
  }

  #apply(view: View): void {
    this.pxPerSecond = view.pxPerSecond;
    this.scrollLeft = view.scrollLeft;
  }

  /** The viewport or the project length changed. */
  setSize(width: number, duration: number): void {
    this.width = width;
    this.duration = duration;
    playhead.duration = duration;
    if (width <= 0) return;
    if (this.fitted) this.#apply(fitView(duration, width));
    else {
      const px = clampZoom(this.pxPerSecond, duration, width);
      this.#apply({
        pxPerSecond: px,
        scrollLeft: clampScroll(this.scrollLeft, duration, px, width),
      });
    }
  }

  fit(): void {
    this.fitted = true;
    this.#apply(fitView(this.duration, this.width));
  }

  /** Zoom by `factor` around screen x `anchorX`; without one, around the playhead if it is on screen, else the middle. */
  zoomBy(factor: number, anchorX?: number): void {
    if (this.width <= 0) return;
    const playheadX = timeToX(playhead.time, this.view);
    const anchor =
      anchorX ?? (playheadX >= 0 && playheadX <= this.width ? playheadX : this.width / 2);
    const next = zoomAt(this.view, factor, anchor, this.duration, this.width);
    this.fitted = next.pxPerSecond <= fitView(this.duration, this.width).pxPerSecond * 1.0001;
    this.#apply(next);
  }

  /** Zoom to an absolute px per second (the slider), around the playhead or the middle. */
  zoomTo(pxPerSecond: number): void {
    this.zoomBy(pxPerSecond / this.pxPerSecond);
  }

  scrollTo(scrollLeft: number): void {
    this.scrollLeft = clampScroll(scrollLeft, this.duration, this.pxPerSecond, this.width);
  }

  scrollBy(delta: number): void {
    this.scrollTo(this.scrollLeft + delta);
  }
}

export const timelineView = new TimelineViewStore();
