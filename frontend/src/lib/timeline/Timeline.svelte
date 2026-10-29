<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { announce } from '../a11y/announce';
  import { registerAction, shortcuts } from '../a11y/dispatcher';
  import { formatClock, formatLength } from '../format';
  import { assets } from '../state/assets.svelte';
  import { clipSelection } from '../state/clipSelection.svelte';
  import { hasAssetDrag, readAssetDrag } from '../state/dnd';
  import { playhead } from '../state/playhead.svelte';
  import { project } from '../state/project.svelte';
  import { rangeSelection } from '../state/rangeSelection.svelte';
  import { timelineView } from '../state/timelineView.svelte';
  import { reportEditError } from '../state/editErrors';
  import { waveforms } from '../state/waveforms.svelte';
  import type { ClipOut } from '../api/client';
  import { ClipDrag } from './drag.svelte';
  import {
    dropOps,
    keyboardStep,
    placeOps,
    reorderOps,
    trimClip,
    trimLimits,
    trimOps,
    type EditOp,
  } from './edits';
  import { buildLanes, clipName, laneAt, lanesHeight, mainTrack, type Lane } from './lanes';
  import LaneHeader from './LaneHeader.svelte';
  import LaneView from './LaneView.svelte';
  import Ruler from './Ruler.svelte';
  import { sourceToTimeline } from './rangeMap';
  import { timeToX, xToTime } from './scale';

  // The timeline: ruler, lanes with their headers, the playhead and the scrollbar (docs/ux-spec.md#timeline). It draws
  // what `project.timeline` says and turns pointer and key gestures into engine operations (`project.applyOps`): no
  // editing rule lives here. Layout is `time * pxPerSecond - scrollLeft` for everything, and only what is on screen is
  // an element, so a three-hour project with hundreds of clips costs the same as a short one.
  const lanes = $derived(buildLanes(project.timeline));
  const totalHeight = $derived(lanesHeight(lanes));
  const duration = $derived(project.timeline?.duration ?? project.project?.duration ?? 0);
  const view = $derived({
    pxPerSecond: timelineView.pxPerSecond,
    scrollLeft: timelineView.scrollLeft,
  });
  const mainClips = $derived(mainTrack(project.timeline)?.clips ?? []);

  let width = $state(0);
  let lanesEl: HTMLDivElement | undefined = $state();
  let scrollEl: HTMLDivElement | undefined = $state();
  let dropLaneId = $state<string | null>(null);

  // --- the view follows the window and the project's length; a new project starts fitted -----------------------
  $effect(() => {
    const w = width;
    const d = duration;
    untrack(() => timelineView.setSize(w, d));
  });
  $effect(() => {
    void project.id;
    untrack(() => {
      timelineView.fitted = true;
      playhead.reset();
      clipSelection.clear();
      waveforms.reset();
    });
  });
  $effect(() => {
    const ids = new Set(lanes.flatMap((lane) => lane.clips.map((c) => c.id)));
    clipSelection.prune(ids);
    // A drag of a clip that someone else just removed would only end in a conflict: drop it.
    const dragged = untrack(() => drag.active?.clipId);
    if (dragged && !ids.has(dragged)) untrack(() => drag.cancel());
  });

  // --- the scrollbar: a native scroller as wide as the content, kept equal to the view ---------------------------
  const contentWidth = $derived(Math.max(width, duration * timelineView.pxPerSecond));
  $effect(() => {
    const left = timelineView.scrollLeft;
    void contentWidth;
    if (scrollEl && Math.abs(scrollEl.scrollLeft - left) > 0.5) scrollEl.scrollLeft = left;
  });

  // --- wheel: Ctrl+wheel zooms around the pointer, Shift+wheel or a sideways wheel scrolls ------------------------
  let pendingFactor = 1;
  let pendingAnchor = 0;
  let zoomFrame = 0;
  function queueZoom(factor: number, anchorX: number): void {
    pendingFactor *= factor;
    pendingAnchor = anchorX;
    if (zoomFrame) return;
    zoomFrame = requestAnimationFrame(() => {
      zoomFrame = 0;
      const f = pendingFactor;
      pendingFactor = 1;
      timelineView.zoomBy(f, pendingAnchor);
    });
  }

  function wheel(node: HTMLElement) {
    const handler = (event: WheelEvent) => {
      const left = lanesEl?.getBoundingClientRect().left ?? 0;
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        queueZoom(Math.exp(-event.deltaY * 0.0025), event.clientX - left);
      } else if (event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        event.preventDefault();
        timelineView.scrollBy(event.deltaX || event.deltaY);
      }
    };
    node.addEventListener('wheel', handler, { passive: false });
    return { destroy: () => node.removeEventListener('wheel', handler) };
  }

  // --- the playhead: press or drag on the ruler or its head ---------------------------------------------------------
  function startSeek(event: PointerEvent, left: number): void {
    if (event.button !== 0) return;
    const target = event.currentTarget as HTMLElement;
    target.setPointerCapture(event.pointerId);
    const to = (clientX: number) => playhead.seek(xToTime(clientX - left, view));
    to(event.clientX);
    let frame = 0;
    let lastX = event.clientX;
    const move = (e: PointerEvent) => {
      lastX = e.clientX;
      if (!frame)
        frame = requestAnimationFrame(() => {
          frame = 0;
          to(lastX);
        });
    };
    const up = () => {
      target.removeEventListener('pointermove', move);
      target.removeEventListener('pointerup', up);
      target.removeEventListener('pointercancel', up);
      if (frame) cancelAnimationFrame(frame);
      to(lastX);
    };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
    target.addEventListener('pointercancel', up);
  }

  /** A press on empty lane space moves the playhead there and deselects (ux-spec decision 1). */
  function onLanesPress(event: PointerEvent): void {
    if (event.button !== 0 || (event.target as Element).closest('[data-clip-id]')) return;
    const left = lanesEl?.getBoundingClientRect().left ?? 0;
    playhead.seek(xToTime(event.clientX - left, view));
    clipSelection.clear();
  }

  // --- editing: every gesture ends in `commit`, one engine call -----------------------------------------------------
  async function commit(ops: EditOp[], what: string, subject = ''): Promise<boolean> {
    const ok = await project.applyOps(ops);
    if (ok) {
      const edited = ops.find((op) => typeof op.clip === 'string')?.clip as string | undefined;
      const clip = edited ? lanes.flatMap((l) => l.clips).find((c) => c.id === edited) : undefined;
      if (clip) {
        announce(
          `${what} ${subject || clipName(clip)}, now ${formatLength(clip.duration)} long, starts at ${formatClock(clip.start)}`,
        );
      } else announce(`${what} ${subject}`.trim());
    } else if (project.editError) {
      reportEditError();
    }
    return ok;
  }

  const drag = new ClipDrag({
    pxPerSecond: () => timelineView.pxPerSecond,
    lanes: () => lanes,
    laneAtY: (y) => laneAt(lanes, y),
    commit: (ops, what) => commit(ops, what),
  });

  let lastPointer = { x: 0, y: 0 };
  let dragFrame = 0;

  function laneY(clientY: number): number {
    return clientY - (lanesEl?.getBoundingClientRect().top ?? 0);
  }

  function onPress(
    lane: Lane,
    clip: ClipOut,
    event: PointerEvent,
    kind: 'move' | 'trim-start' | 'trim-end',
  ): void {
    if (event.button !== 0) return;
    clipSelection.select(clip.id, event.shiftKey || event.ctrlKey || event.metaKey);
    if (lane.track.locked || lane.kind === 'main-audio') return;
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    lastPointer = { x: event.clientX, y: event.clientY };
    drag.begin(kind, lane, clip, event.clientX, event.clientY);
  }

  function onMove(event: PointerEvent): void {
    if (!drag.active) return;
    lastPointer = { x: event.clientX, y: event.clientY };
    if (dragFrame) return;
    dragFrame = requestAnimationFrame(() => {
      dragFrame = 0;
      drag.update(lastPointer.x, lastPointer.y, laneY(lastPointer.y));
    });
  }

  async function onRelease(event: PointerEvent): Promise<void> {
    if (!drag.active) return;
    if (dragFrame) cancelAnimationFrame(dragFrame);
    dragFrame = 0;
    drag.update(event.clientX, event.clientY, laneY(event.clientY));
    await drag.finish(); // commits one edit; `commit` announces it or shows the engine's refusal
  }

  // --- keyboard: arrows move between clips, Alt+arrows move, Shift+Alt+arrows trim -----------------------------------
  async function focusClip(laneId: string, clipId: string): Promise<void> {
    const lane = lanes.find((l) => l.id === laneId);
    const clip = lane?.clips.find((c) => c.id === clipId);
    if (!clip) return;
    const x = timeToX(clip.start, view);
    if (x < 0 || timeToX(clip.end, view) > width)
      timelineView.scrollTo(clip.start * view.pxPerSecond - 48);
    await tick();
    lanesEl
      ?.querySelector<HTMLElement>(
        `[data-lane-id="${CSS.escape(laneId)}"] [data-clip-id="${CSS.escape(clipId)}"]`,
      )
      ?.focus();
  }

  function onKey(lane: Lane, clip: ClipOut, event: KeyboardEvent): void {
    const index = lane.clips.findIndex((c) => c.id === clip.id);
    const direction = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    const primary = event.ctrlKey || event.metaKey;
    let handled = true;
    if (direction !== 0 && event.altKey) {
      if (lane.track.locked || lane.kind === 'main-audio') {
        announce(`${lane.name} is locked or cannot be edited here`);
      } else if (event.shiftKey) {
        const edge = primary ? 'start' : 'end';
        const trimmed = trimClip(
          clip,
          edge,
          direction * keyboardStep(timelineView.pxPerSecond),
          trimLimits(lane, clip),
        );
        const ops = trimOps(clip, edge, trimmed);
        if (ops.length) void commit(ops, 'Trimmed').then(() => focusClip(lane.id, clip.id));
      } else if (lane.track.magnetic) {
        const to = index + direction;
        if (to >= 0 && to < lane.clips.length) {
          void commit(reorderOps(lane.clips, index, to), 'Moved').then(() => {
            announce(`Moved ${clipName(clip)} to position ${to + 1} of ${lane.clips.length}`);
            return focusClip(lane.id, clip.id);
          });
        }
      } else {
        const ops = placeOps(
          clip,
          lane.track.id,
          direction * keyboardStep(timelineView.pxPerSecond),
          lane.track.id,
        );
        if (ops.length) void commit(ops, 'Moved').then(() => focusClip(lane.id, clip.id));
      }
    } else if (direction !== 0 && !event.shiftKey && !primary) {
      const next = lane.clips[index + direction];
      if (next) {
        clipSelection.select(next.id);
        void focusClip(lane.id, next.id);
      }
    } else if (event.key === 'Home' || event.key === 'End') {
      const target = event.key === 'Home' ? lane.clips[0] : lane.clips[lane.clips.length - 1];
      if (target) {
        clipSelection.select(target.id);
        void focusClip(lane.id, target.id);
      }
    } else if (event.key === 'Enter' || event.key === ' ') {
      clipSelection.select(clip.id, event.shiftKey);
    } else handled = false;
    if (handled) event.preventDefault();
  }

  // --- lane toggles --------------------------------------------------------------------------------------------------
  function toggle(lane: Lane, flag: 'locked' | 'hidden' | 'muted'): void {
    const next = !lane.track[flag];
    const word = {
      locked: next ? 'Locked' : 'Unlocked',
      hidden: next ? 'Hid' : 'Showed',
      muted: next ? 'Muted' : 'Unmuted',
    }[flag];
    void commit([{ op: 'set_track_flags', track: lane.track.id, [flag]: next }], word, lane.name);
  }

  // --- dropping a card from the Media tab (the contract is in state/dnd.ts) -------------------------------------------
  function onDragOver(event: DragEvent): void {
    if (!hasAssetDrag(event.dataTransfer)) return;
    event.preventDefault(); // accept the drop
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    dropLaneId = laneAt(lanes, laneY(event.clientY))?.id ?? null;
  }

  async function onDrop(event: DragEvent): Promise<void> {
    dropLaneId = null;
    const payload = readAssetDrag(event.dataTransfer);
    if (!payload) return;
    event.preventDefault();
    const asset = assets.items.find((a) => a.id === payload.assetId);
    const left = lanesEl?.getBoundingClientRect().left ?? 0;
    const time = xToTime(event.clientX - left, view);
    const lane = laneAt(lanes, laneY(event.clientY));
    const ops = dropOps(
      { id: payload.assetId, has_video: asset?.has_video ?? true },
      lane,
      time,
      lanes,
      mainClips,
    );
    await commit(ops, 'Added', `${payload.name} to the timeline`);
  }

  // --- shortcuts from the registry --------------------------------------------------------------------------------
  $effect(() =>
    registerAction('timeline.zoom', ({ key }) => timelineView.zoomBy(key === '-' ? 1 / 1.4 : 1.4)),
  );
  $effect(() => registerAction('timeline.fit', () => timelineView.fit()));
  $effect(() =>
    registerAction('ui.escape', () => {
      if (shortcuts.currentScope() === 'dialog') return false;
      if (drag.active) return drag.cancel();
      if (clipSelection.ids.length === 0) return false;
      clipSelection.clear();
      announce('Selection cleared');
    }),
  );

  const dragState = $derived(drag.active);
  const dragLane = $derived(dragState ? lanes.find((l) => l.id === dragState.laneId) : undefined);
  const playheadX = $derived(timeToX(playhead.time, view));
  const empty = $derived(project.timeline !== null && mainClips.length === 0);
  // The highlight under the pointer or picked in the Highlights tab, shown where the clips have it (source seconds in,
  // timeline seconds out, so cuts and reordering are respected).
  const rangeSpans = $derived.by(() => {
    const range = rangeSelection.range;
    return range ? sourceToTimeline(mainClips, range.assetId, range.start, range.end) : [];
  });
</script>

<div
  class="timeline"
  data-testid="timeline"
  data-px-per-second={timelineView.pxPerSecond}
  data-scroll-left={timelineView.scrollLeft}
  data-duration={duration}
  data-width={width}
>
  <div class="corner"></div>
  <div class="ruler-cell" use:wheel>
    <Ruler {view} {width} onseek={startSeek} />
  </div>

  <div class="rows" data-testid="rows">
    <div class="heads" style:height="{totalHeight}px" data-testid="lane-headers">
      {#each lanes as lane (lane.id)}
        <div class="head-slot" style:top="{lane.top}px">
          <LaneHeader {lane} ontoggle={(flag) => toggle(lane, flag)} />
        </div>
      {/each}
    </div>
    <!-- Dropping is one way in; the Add button on a card is the keyboard way. Pressing the empty lane moves the playhead. -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="lanes"
      class:over={dropLaneId !== null}
      style:height="{totalHeight}px"
      bind:this={lanesEl}
      bind:clientWidth={width}
      use:wheel
      data-testid="lanes"
      data-drop-target="asset"
      onpointerdown={onLanesPress}
      ondragover={onDragOver}
      ondragleave={() => (dropLaneId = null)}
      ondrop={onDrop}
    >
      {#each lanes as lane (lane.id)}
        <LaneView
          {lane}
          {view}
          {width}
          drag={dragState}
          dropTarget={dropLaneId === lane.id}
          onpress={onPress}
          onmove={onMove}
          onrelease={onRelease}
          oncancel={() => drag.cancel()}
          onkey={onKey}
        />
      {/each}
      {#if dragState?.marker != null && dragLane}
        <div
          class="marker"
          style:left="{timeToX(dragState.marker, view)}px"
          style:top="{dragLane.top}px"
          style:height="{dragLane.height}px"
          data-testid="drop-marker"
        ></div>
      {/if}
      {#if dragState?.moved && dragState.hint && dragLane}
        <div
          class="hint"
          style:left="{Math.max(0, timeToX(dragState.start, view))}px"
          style:top="{Math.max(0, dragLane.top - 22)}px"
          data-testid="drag-hint"
        >
          {dragState.hint}
        </div>
      {/if}
      {#each rangeSpans as span (span.start)}
        <div
          class="range"
          style:left="{timeToX(span.start, view)}px"
          style:width="{Math.max(2, (span.end - span.start) * view.pxPerSecond)}px"
          style:height="{totalHeight}px"
          data-testid="range-overlay"
          data-start={span.start}
          data-end={span.end}
          aria-hidden="true"
        ></div>
      {/each}
      {#if empty}
        <p class="empty" data-testid="timeline-empty">Drag material here and start to create</p>
      {/if}
    </div>
  </div>

  <div class="overlay">
    {#if playheadX >= -6 && playheadX <= width + 6}
      <div
        class="playhead"
        style:left="{playheadX}px"
        data-testid="playhead"
        data-time={playhead.time}
      >
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div
          class="grip"
          onpointerdown={(event) => startSeek(event, lanesEl?.getBoundingClientRect().left ?? 0)}
        ></div>
      </div>
    {/if}
  </div>

  <div
    class="hscroll"
    bind:this={scrollEl}
    onscroll={() =>
      scrollEl &&
      Math.abs(scrollEl.scrollLeft - timelineView.scrollLeft) > 1 &&
      timelineView.scrollTo(scrollEl.scrollLeft)}
    aria-hidden="true"
    data-testid="hscroll"
  >
    <div style:width="{contentWidth}px" style:height="1px"></div>
  </div>
</div>

<style>
  .timeline {
    display: grid;
    grid-template-columns: var(--size-lane-header-width) minmax(0, 1fr);
    grid-template-rows: var(--size-ruler-height) minmax(0, 1fr) var(--size-timeline-scrollbar);
    flex: 1;
    min-height: 0;
    min-width: 0;
  }

  .corner {
    grid-column: 1;
    grid-row: 1;
    border-right: 1px solid var(--color-border);
    border-bottom: 1px solid var(--color-border);
    box-sizing: border-box;
  }

  .ruler-cell {
    grid-column: 2;
    grid-row: 1;
    min-width: 0;
  }

  .rows {
    grid-column: 1 / 3;
    grid-row: 2;
    display: flex;
    min-height: 0;
    overflow-x: hidden;
    overflow-y: auto;
    scrollbar-width: none;
  }

  .heads {
    position: relative;
    flex: none;
    width: var(--size-lane-header-width);
    box-sizing: border-box;
    border-right: 1px solid var(--color-border);
  }

  .head-slot {
    position: absolute;
    left: 0;
    right: 0;
  }

  .lanes {
    position: relative;
    flex: 1;
    min-width: 0;
    overflow: hidden;
  }

  .lanes.over {
    outline: 2px dashed var(--color-accent);
    outline-offset: -2px;
  }

  .overlay {
    grid-column: 2;
    grid-row: 1 / 3;
    position: relative;
    overflow: hidden;
    pointer-events: none;
    z-index: 4;
  }

  .playhead {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    margin-left: -1px;
    background: var(--color-playhead);
  }

  .grip {
    position: absolute;
    top: 0;
    left: -5px;
    width: 12px;
    height: 12px;
    background: var(--color-playhead);
    clip-path: polygon(0 0, 100% 0, 50% 100%);
    cursor: col-resize;
    pointer-events: auto;
    touch-action: none;
  }

  .range {
    position: absolute;
    top: 0;
    border-right: 2px solid var(--color-accent);
    border-left: 2px solid var(--color-accent);
    background: color-mix(in srgb, var(--color-accent) 22%, transparent);
    box-sizing: border-box;
    pointer-events: none;
    z-index: 3;
  }

  .marker {
    position: absolute;
    width: 3px;
    margin-left: -1px;
    background: var(--color-accent);
    pointer-events: none;
    z-index: 4;
  }

  .hint {
    position: absolute;
    padding: 1px var(--space-2);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-sm);
    background: var(--color-surface-raised);
    color: var(--color-text);
    font-size: var(--font-size-xs);
    white-space: nowrap;
    pointer-events: none;
    z-index: 5;
  }

  .hscroll {
    grid-column: 2;
    grid-row: 3;
    overflow-x: scroll;
    overflow-y: hidden;
    scrollbar-width: thin;
  }

  .empty {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    margin: 0;
    color: var(--color-text-muted);
    pointer-events: none;
  }
</style>
