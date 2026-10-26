<script lang="ts">
  import { timelineView } from '../state/timelineView.svelte';
  import { sliderToZoom, zoomToSlider } from '../timeline/scale';
  import Timeline from '../timeline/Timeline.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Slider from '../ui/Slider.svelte';

  // The timeline region: the toolbar (ux-spec #timeline) and the timeline itself (lib/timeline). The editing tools
  // that need history or the preview (undo, redo, split, delete) are placeholders until their cards.
  const zoomValue = $derived(
    zoomToSlider(timelineView.pxPerSecond, timelineView.duration, timelineView.width),
  );
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
    <span class="spacer"></span>
    <IconButton
      label="Zoom out"
      icon="minus"
      action="timeline.zoom"
      size="sm"
      onclick={() => timelineView.zoomBy(1 / 1.4)}
    />
    <div class="zoom">
      <Slider
        label="Zoom"
        inline
        min={0}
        max={100}
        step={1}
        format={(v) => `${Math.round(v)}%`}
        bind:value={
          () => zoomValue,
          (v) => timelineView.zoomTo(sliderToZoom(v, timelineView.duration, timelineView.width))
        }
        data-testid="zoom-slider"
      />
    </div>
    <IconButton
      label="Zoom in"
      icon="plus"
      action="timeline.zoom"
      size="sm"
      onclick={() => timelineView.zoomBy(1.4)}
    />
    <IconButton
      label="Fit the whole timeline"
      icon="fit"
      action="timeline.fit"
      size="sm"
      onclick={() => timelineView.fit()}
    />
  </div>
  <Timeline />
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
    flex: none;
    align-items: center;
    gap: var(--space-1);
    height: var(--size-toolbar-height);
    padding: 0 var(--space-3);
    border-bottom: 1px solid var(--color-border);
  }

  .spacer {
    flex: 1;
  }

  .zoom {
    width: 200px;
  }
</style>
