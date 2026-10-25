<script lang="ts">
  import { formatClock } from '../format';
  import { project } from '../state/project.svelte';
  import IconButton from '../ui/IconButton.svelte';

  // The timeline region: toolbar, ruler and the lane frame with its headers (ux-spec #timeline). The lanes hold
  // the empty state until the timeline cards draw clips. The ruler is real: it spans the project's length.
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
    <div class="lanes" data-testid="lanes">
      <div class="ruler" aria-hidden="true">
        {#each ticks as tick (tick.left)}
          <span style:left="{tick.left}%">{tick.label}</span>
        {/each}
      </div>
      <div class="lane"></div>
      <div class="lane"></div>
      <p class="empty" data-testid="timeline-empty">Drag material here and start to create</p>
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
