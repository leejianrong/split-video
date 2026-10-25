<script lang="ts">
  import { waveforms } from '../state/waveforms.svelte';
  import type { Lane } from './lanes';
  import { timeToX, visibleRange, visibleSlice, xToTime, type View } from './scale';
  import { waveformColumns } from './waveform';

  // The waveform of the main track's audio, drawn on one canvas as wide as the lanes: only the columns on screen, from
  // the curves the server stored (the browser never decodes audio). Where a clip has no curve yet the lane says so.
  let { lane, view, width }: { lane: Lane; view: View; width: number } = $props();

  const BAR = 2; // px per column
  let canvas: HTMLCanvasElement | undefined = $state();
  let frame = 0;

  const status = $derived.by(() => {
    const range = visibleRange(view, width);
    const [lo, hi] = visibleSlice(lane.clips, range.start, range.end);
    const states = lane.clips
      .slice(lo, hi)
      .map((c) => waveforms.entries[c.asset]?.status ?? 'loading');
    if (states.some((s) => s === 'computing' || s === 'loading'))
      return 'Preparing the waveform...';
    if (states.length > 0 && states.every((s) => s === 'none')) return 'No audio';
    if (states.some((s) => s === 'failed')) return 'The waveform could not be made';
    return '';
  });

  // Ask for the curves of the clips on screen (a recording's own: one request however many segments it has).
  $effect(() => {
    const range = visibleRange(view, width);
    const [lo, hi] = visibleSlice(lane.clips, range.start, range.end);
    for (const clip of lane.clips.slice(lo, hi)) if (clip.asset) waveforms.ensure(clip.asset);
  });

  function draw(): void {
    frame = 0;
    const el = canvas;
    if (!el || width <= 0) return;
    const dpr = window.devicePixelRatio || 1;
    const h = lane.height;
    if (el.width !== Math.round(width * dpr) || el.height !== Math.round(h * dpr)) {
      el.width = Math.round(width * dpr);
      el.height = Math.round(h * dpr);
    }
    const ctx = el.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, h);
    ctx.fillStyle = getComputedStyle(el).color; // the canvas's colour is the waveform token
    const mid = h / 2;
    const half = mid - 4;
    const range = visibleRange(view, width);
    const [lo, hi] = visibleSlice(lane.clips, range.start, range.end);
    for (const clip of lane.clips.slice(lo, hi)) {
      const data = waveforms.entries[clip.asset]?.data;
      if (!data) continue;
      const x0 = Math.max(0, Math.floor(timeToX(clip.start, view)));
      const x1 = Math.min(width, Math.ceil(timeToX(clip.end, view)));
      const count = Math.ceil((x1 - x0) / BAR);
      if (count <= 0) continue;
      const from = clip.in_point + (xToTime(x0, view) - clip.start);
      const to = clip.in_point + (xToTime(x1, view) - clip.start);
      const columns = waveformColumns(data, from, to, count);
      for (let i = 0; i < columns.length; i++) {
        const c = columns[i]!;
        const top = mid - c.max * half;
        const bottom = mid - c.min * half;
        ctx.fillRect(x0 + i * BAR, top, BAR - 0.5, Math.max(1, bottom - top));
      }
    }
  }

  // Redraw when the view, the lane or a curve changes; one draw per frame however often that happens.
  $effect(() => {
    void [
      view.pxPerSecond,
      view.scrollLeft,
      width,
      lane.clips,
      lane.height,
      waveforms.entries,
      canvas,
    ];
    if (!frame) frame = requestAnimationFrame(draw);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
    };
  });
</script>

<canvas
  bind:this={canvas}
  style:width="{width}px"
  style:height="{lane.height}px"
  data-testid="waveform"
></canvas>
{#if status}
  <span class="status" aria-hidden="true" data-testid="waveform-status">{status}</span>
{/if}

<style>
  canvas {
    position: absolute;
    inset: 0 auto auto 0;
    pointer-events: none;
    color: var(--color-waveform);
  }

  .status {
    position: absolute;
    left: var(--space-3);
    top: 50%;
    transform: translateY(-50%);
    padding: 0 var(--space-2);
    border-radius: var(--radius-sm);
    background: var(--color-surface);
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
    pointer-events: none;
  }
</style>
