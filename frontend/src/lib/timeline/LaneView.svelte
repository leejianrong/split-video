<script lang="ts">
  import type { ClipOut } from '../api/client';
  import { clipSelection } from '../state/clipSelection.svelte';
  import ClipView from './ClipView.svelte';
  import type { DragState } from './drag.svelte';
  import type { Lane } from './lanes';
  import { visibleRange, visibleSlice, type View } from './scale';
  import WaveformCanvas from './WaveformCanvas.svelte';

  // One lane row: only the clips that touch the screen (plus a margin) are elements, found with two binary searches
  // on the lane's sorted clips. The lane is a listbox of its clips (ux-spec #accessibility): one clip is in the tab
  // order, arrows move between them (the timeline handles the keys).
  let {
    lane,
    view,
    width,
    drag,
    dropTarget,
    onpress,
    onmove,
    onrelease,
    oncancel,
    onkey,
  }: {
    lane: Lane;
    view: View;
    width: number;
    drag: DragState | null;
    dropTarget: boolean;
    onpress: (
      lane: Lane,
      clip: ClipOut,
      event: PointerEvent,
      kind: 'move' | 'trim-start' | 'trim-end',
    ) => void;
    onmove: (event: PointerEvent) => void;
    onrelease: (event: PointerEvent) => void;
    oncancel: () => void;
    onkey: (lane: Lane, clip: ClipOut, event: KeyboardEvent) => void;
  } = $props();

  const OVERSCAN_PX = 240;
  const range = $derived(visibleRange(view, width, OVERSCAN_PX));
  const slice = $derived(visibleSlice(lane.clips, range.start, range.end));
  const shown = $derived(lane.clips.slice(slice[0], slice[1]));
  // The dragged clip stays mounted even if the pointer took it out of the window.
  const dragged = $derived(
    drag && drag.laneId === lane.id && !shown.some((c) => c.id === drag.clipId)
      ? lane.clips.find((c) => c.id === drag.clipId)
      : undefined,
  );
  const rendered = $derived(dragged ? [...shown, dragged] : shown);
  const activeId = $derived(
    lane.clips.find((c) => clipSelection.has(c.id) && shown.includes(c))?.id ??
      shown[0]?.id ??
      null,
  );
</script>

<div
  class="lane kind-{lane.kind}"
  class:locked={lane.track.locked}
  class:hidden={lane.track.hidden}
  class:muted={lane.track.muted}
  class:target={dropTarget}
  role="listbox"
  aria-label="{lane.name}{lane.track.locked ? ' (locked)' : ''}"
  aria-orientation="horizontal"
  aria-multiselectable="true"
  style:top="{lane.top}px"
  style:height="{lane.height}px"
  data-testid="lane"
  data-lane-id={lane.id}
  data-kind={lane.kind}
>
  {#if lane.kind === 'main-audio'}
    <WaveformCanvas {lane} {view} {width} />
  {/if}
  {#each rendered as clip (clip.id)}
    <ClipView
      {lane}
      {clip}
      {view}
      selected={clipSelection.has(clip.id)}
      active={clip.id === activeId}
      drag={drag?.clipId === clip.id ? drag : null}
      visibleFrom={range.start}
      visibleTo={range.end}
      onpress={(event, kind) => onpress(lane, clip, event, kind)}
      {onmove}
      {onrelease}
      {oncancel}
      onkey={(event) => onkey(lane, clip, event)}
    />
  {/each}
</div>

<style>
  .lane {
    position: absolute;
    left: 0;
    right: 0;
    box-sizing: border-box;
    border-bottom: 1px solid var(--color-border);
  }

  .kind-main-audio {
    background: var(--color-bg);
  }

  .hidden {
    opacity: 0.45;
  }

  .muted :global(canvas) {
    opacity: 0.35;
  }

  .target {
    background: var(--color-accent-subtle);
  }
</style>
