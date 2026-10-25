<script lang="ts">
  import { announce } from '../a11y/announce';
  import { baseName, formatClock, formatDuration } from '../format';
  import { hasAssetDrag, readAssetDrag } from '../state/dnd';
  import { project } from '../state/project.svelte';
  import { toasts } from '../state/toasts.svelte';
  import IconButton from '../ui/IconButton.svelte';

  // The timeline region: toolbar, ruler and the lane frame with its headers (ux-spec #timeline). The lanes hold
  // the empty state until the timeline cards draw clips. The ruler is real: it spans the project's length.
  // Interim (SPL-63), replaced by the lanes of SPL-65: the clips of the main video track as a plain list, so an asset
  // added from the Media tab (button, double click or a drag dropped on the lanes) can be seen to land.
  const mainClips = $derived(project.timeline?.tracks.find((t) => t.kind === 'video')?.clips ?? []);
  let over = $state(false);

  function onDragOver(event: DragEvent) {
    if (!hasAssetDrag(event.dataTransfer)) return;
    event.preventDefault(); // accept the drop
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    over = true;
  }

  async function onDrop(event: DragEvent) {
    over = false;
    const payload = readAssetDrag(event.dataTransfer);
    if (!payload) return;
    event.preventDefault();
    if (await project.addAsset(payload.assetId)) {
      announce(`Added ${payload.name} to the timeline`);
    } else if (project.editError) {
      toasts.error(`${project.editError.message} ${project.editError.fix}`.trim());
    }
  }

  const ticks = $derived.by(() => {
    const total = project.project?.duration ?? 0;
    return Array.from({ length: 6 }, (_, i) => ({
      left: (i / 6) * 100,
      label: formatClock((total * i) / 6),
    }));
  });
</script>

<section class="timeline" data-region="timeline" aria-label="Timeline">
  <div class="toolbar" role="toolbar" aria-label="Timeline tools">
    <IconButton label="Undo" icon="undo" action="history.undo" size="sm" disabled />
    <IconButton label="Redo" icon="redo" action="history.redo" size="sm" disabled />
    <IconButton
      label="Split at the playhead"
      icon="scissors"
      action="timeline.split"
      size="sm"
      disabled
    />
    <IconButton label="Delete" icon="trash" action="timeline.delete" size="sm" disabled />
  </div>
  <div class="body">
    <div class="heads" data-testid="lane-headers">
      <span class="ruler-gap"></span>
      <span class="head">Main · Video</span>
      <span class="head">Main · Audio</span>
    </div>
    <!-- Dropping is one way in; the Add button on a card is the keyboard way. -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="lanes"
      class:over
      data-testid="lanes"
      data-drop-target="asset"
      ondragover={onDragOver}
      ondragleave={() => (over = false)}
      ondrop={onDrop}
    >
      <div class="ruler" aria-hidden="true">
        {#each ticks as tick (tick.left)}
          <span style:left="{tick.left}%">{tick.label}</span>
        {/each}
      </div>
      <div class="lane">
        <ol class="clips" aria-label="Clips on the main track" data-testid="timeline-clips">
          {#each mainClips as clip (clip.id)}
            <li
              class="clip"
              data-testid="timeline-clip"
              data-asset={clip.asset}
              title={clip.source}
            >
              <span class="clip-name">{clip.label || baseName(clip.source)}</span>
              <span class="clip-len">{formatDuration(clip.duration)}</span>
            </li>
          {/each}
        </ol>
      </div>
      <div class="lane"></div>
      {#if mainClips.length === 0}
        <p class="empty" data-testid="timeline-empty">Drag material here and start to create</p>
      {/if}
    </div>
  </div>
</section>

<style>
  .timeline {
    display: flex;
    flex-direction: column;
    position: relative;
    min-width: 0;
    min-height: 0;
    border-top: 1px solid var(--color-border);
    background: var(--color-surface);
  }

  .toolbar {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    height: var(--size-toolbar-height);
    padding: 0 var(--space-3);
    border-bottom: 1px solid var(--color-border);
  }

  .body {
    display: flex;
    flex: 1;
    min-height: 0;
  }

  .heads {
    display: flex;
    flex-direction: column;
    flex: none;
    width: var(--size-lane-header-width);
    border-right: 1px solid var(--color-border);
  }

  .ruler-gap {
    height: 22px;
    border-bottom: 1px solid var(--color-border);
  }

  .head {
    display: flex;
    align-items: center;
    height: 44px;
    padding: 0 var(--space-3);
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
  }

  .lanes {
    position: relative;
    flex: 1;
    min-width: 0;
    overflow: hidden;
  }

  .ruler {
    position: relative;
    height: 22px;
    border-bottom: 1px solid var(--color-border);
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
  }

  .ruler span {
    position: absolute;
    top: 2px;
    padding-left: var(--space-1);
    border-left: 1px solid var(--color-border-strong);
    line-height: 16px;
  }

  .lane {
    height: 44px;
    border-bottom: 1px solid var(--color-border);
  }

  .lanes.over {
    outline: 2px dashed var(--color-accent);
    outline-offset: -2px;
  }

  .clips {
    display: flex;
    gap: var(--space-1);
    height: 100%;
    margin: 0;
    padding: var(--space-1);
    overflow: hidden;
    list-style: none;
  }

  .clip {
    display: flex;
    flex: none;
    flex-direction: column;
    justify-content: center;
    max-width: 180px;
    padding: 0 var(--space-2);
    overflow: hidden;
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-md);
    background: var(--color-accent-subtle);
    font-size: var(--font-size-xs);
  }

  .clip-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .clip-len {
    color: var(--color-text-muted);
  }

  .empty {
    position: absolute;
    inset: 22px 0 0;
    display: grid;
    place-items: center;
    margin: 0;
    color: var(--color-text-muted);
    pointer-events: none;
  }
</style>
