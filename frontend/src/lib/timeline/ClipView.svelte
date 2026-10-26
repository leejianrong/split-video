<script lang="ts">
  import type { ClipOut } from '../api/client';
  import { formatClock, formatLength } from '../format';
  import Icon from '../ui/icons/Icon.svelte';
  import type { DragState } from './drag.svelte';
  import Filmstrip from './Filmstrip.svelte';
  import { clipName, clipTone, type Lane } from './lanes';
  import { timeToX, type View } from './scale';

  // One clip box on a lane: a labelled, coloured rectangle at `start * pxPerSecond - scrollLeft`, with a filmstrip on
  // the main video track and a handle on each edge to trim. The press / move / release callbacks belong to the
  // timeline, which runs one drag at a time; the box only draws what the drag says (`drag`).
  let {
    lane,
    clip,
    view,
    selected,
    active,
    drag,
    visibleFrom,
    visibleTo,
    onpress,
    onmove,
    onrelease,
    oncancel,
    onkey,
  }: {
    lane: Lane;
    clip: ClipOut;
    view: View;
    selected: boolean;
    /** The one clip of the lane that is in the tab order (roving tabindex). */
    active: boolean;
    drag: DragState | null;
    /** The on-screen part of the timeline, in seconds. */
    visibleFrom: number;
    visibleTo: number;
    onpress: (event: PointerEvent, kind: 'move' | 'trim-start' | 'trim-end') => void;
    onmove: (event: PointerEvent) => void;
    onrelease: (event: PointerEvent) => void;
    oncancel: () => void;
    onkey: (event: KeyboardEvent) => void;
  } = $props();

  const audio = $derived(lane.kind === 'main-audio');
  const locked = $derived(lane.track.locked);
  const start = $derived(drag ? drag.start : clip.start);
  const end = $derived(drag ? drag.end : clip.end);
  const left = $derived(timeToX(start, view));
  const width = $derived(Math.max(1, (end - start) * view.pxPerSecond));
  const name = $derived(clipName(clip));
  const tone = $derived(clipTone(lane, clip));
  const filmstrip = $derived(lane.kind === 'video' && clip.kind === 'media' && !!clip.asset);
  const fromSource = $derived(clip.in_point + (Math.max(visibleFrom, clip.start) - clip.start));
  const toSource = $derived(clip.in_point + (Math.min(visibleTo, clip.end) - clip.start));
</script>

<div
  class="clip tone-{tone}"
  class:selected
  class:dragging={drag !== null}
  class:locked
  class:audio
  class:filmstrip
  role="option"
  aria-selected={selected}
  aria-label="{name}, {formatLength(clip.duration)}, starts at {formatClock(clip.start)}{locked
    ? ', locked'
    : ''}"
  tabindex={active ? 0 : -1}
  title={name}
  style:left="{left}px"
  style:width="{width}px"
  style:--clip-color={clip.label_color ?? undefined}
  style:transform={drag && drag.dy ? `translateY(${drag.dy}px)` : undefined}
  data-testid="clip"
  data-clip-id={clip.id}
  data-lane-id={lane.id}
  data-kind={lane.kind}
  data-start={clip.start}
  data-end={clip.end}
  onpointerdown={(event) => onpress(event, 'move')}
  onpointermove={onmove}
  onpointerup={onrelease}
  onpointercancel={oncancel}
  onlostpointercapture={oncancel}
  onkeydown={onkey}
>
  {#if filmstrip}
    <Filmstrip
      clipId={clip.id}
      assetId={clip.asset}
      clipIn={clip.in_point}
      clipOut={clip.out_point}
      pxPerSecond={view.pxPerSecond}
      visibleFrom={fromSource}
      visibleTo={toSource}
      height={lane.height - 8}
    />
  {/if}
  {#if !audio && width >= 36}
    <span class="label">{name}</span>
  {/if}
  {#if locked}
    <span class="lock" aria-hidden="true"><Icon name="lock" size={12} /></span>
  {/if}
  {#if !audio && !locked}
    <span
      class="handle start"
      aria-hidden="true"
      data-testid="trim-start"
      onpointerdown={(event) => {
        event.stopPropagation();
        onpress(event, 'trim-start');
      }}
    ></span>
    <span
      class="handle end"
      aria-hidden="true"
      data-testid="trim-end"
      onpointerdown={(event) => {
        event.stopPropagation();
        onpress(event, 'trim-end');
      }}
    ></span>
  {/if}
</div>

<style>
  .clip {
    position: absolute;
    top: 4px;
    bottom: 4px;
    display: flex;
    align-items: flex-start;
    min-width: 1px;
    overflow: hidden;
    box-sizing: border-box;
    border: 1px solid color-mix(in srgb, var(--color-text) 14%, transparent);
    border-radius: var(--radius-md);
    background: var(--clip-color, var(--clip-bg));
    color: var(--color-text);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    cursor: grab;
    touch-action: none;
    user-select: none;
  }

  .tone-segment {
    --clip-bg: var(--color-clip-segment);
  }
  .tone-song {
    --clip-bg: var(--color-clip-song);
  }
  .tone-overlay {
    --clip-bg: var(--color-clip-overlay);
  }
  .tone-music {
    --clip-bg: var(--color-clip-music);
  }
  .tone-text {
    --clip-bg: var(--color-clip-text);
    color: var(--color-clip-text-ink);
  }

  .audio {
    background: transparent;
    border-color: transparent;
    cursor: default;
  }

  .clip.selected {
    outline: 2px solid var(--color-text);
    outline-offset: -2px;
  }

  .clip:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: -2px;
    z-index: 2;
  }

  .clip.selected:focus-visible {
    outline-color: var(--color-focus);
  }

  .dragging {
    z-index: 3;
    opacity: 0.9;
    cursor: grabbing;
    box-shadow: 0 2px 10px color-mix(in srgb, var(--color-bg) 70%, transparent);
  }

  .locked {
    cursor: not-allowed;
  }

  .label {
    position: relative;
    min-width: 0;
    max-width: 100%;
    padding: 3px var(--space-2);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    pointer-events: none;
  }

  /* A label over a filmstrip needs its own backing to stay readable on any frame. */
  .filmstrip .label {
    margin: 2px;
    padding: 0 var(--space-2);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--color-bg) 62%, transparent);
  }

  .lock {
    position: absolute;
    right: var(--space-1);
    bottom: var(--space-1);
    display: inline-flex;
    pointer-events: none;
  }

  .handle {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 7px;
    cursor: ew-resize;
    z-index: 1;
  }
  .handle.start {
    left: 0;
  }
  .handle.end {
    right: 0;
  }
  .clip:hover .handle,
  .clip.selected .handle {
    background: color-mix(in srgb, var(--color-text) 35%, transparent);
  }
</style>
