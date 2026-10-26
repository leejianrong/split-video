<script lang="ts">
  import { formatClock } from '../format';
  import { playhead } from '../state/playhead.svelte';
  import { rulerTicks, type View } from './scale';

  // The time ruler: labels adapt to the zoom (a second to an hour), only the ticks on screen exist. Press or drag on it
  // to move the playhead; with the keyboard it is a slider (arrows step 1 s, Shift 10 s, Home and End jump).
  let {
    view,
    width,
    onseek,
  }: { view: View; width: number; onseek: (event: PointerEvent, left: number) => void } = $props();

  const ticks = $derived(rulerTicks(view, width));
  let el: HTMLDivElement | undefined = $state();

  function onkeydown(event: KeyboardEvent): void {
    const step = event.shiftKey ? 10 : 1;
    const moves: Record<string, number> = { ArrowLeft: -step, ArrowRight: step };
    if (event.key in moves) playhead.seek(playhead.time + (moves[event.key] ?? 0));
    else if (event.key === 'Home') playhead.seek(0);
    else if (event.key === 'End') playhead.seek(playhead.duration);
    else return;
    event.preventDefault();
  }
</script>

<div
  class="ruler"
  bind:this={el}
  role="slider"
  tabindex="0"
  aria-label="Playhead position"
  aria-valuemin={0}
  aria-valuemax={Math.round(playhead.duration)}
  aria-valuenow={Math.round(playhead.time)}
  aria-valuetext={formatClock(playhead.time)}
  data-testid="ruler"
  onpointerdown={(event) => el && onseek(event, el.getBoundingClientRect().left)}
  {onkeydown}
>
  {#each ticks as tick (tick.time)}
    <span class="tick" class:major={tick.major} style:left="{tick.x}px" data-time={tick.time}>
      {#if tick.label}<span class="label">{tick.label}</span>{/if}
    </span>
  {/each}
</div>

<style>
  .ruler {
    position: relative;
    height: var(--size-ruler-height);
    overflow: hidden;
    border-bottom: 1px solid var(--color-border);
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
    cursor: col-resize;
    user-select: none;
    touch-action: none;
  }

  .tick {
    position: absolute;
    bottom: 0;
    height: 5px;
    border-left: 1px solid var(--color-border-strong);
    pointer-events: none;
  }

  .tick.major {
    height: 100%;
  }

  .label {
    position: absolute;
    top: 2px;
    left: var(--space-1);
    line-height: 16px;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
</style>
