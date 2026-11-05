<script lang="ts">
  import { announce } from '../a11y/announce';
  import { registerAction } from '../a11y/dispatcher';
  import { clipSelection } from '../state/clipSelection.svelte';
  import { reportEditError } from '../state/editErrors';
  import { playhead } from '../state/playhead.svelte';
  import { project } from '../state/project.svelte';
  import { toasts } from '../state/toasts.svelte';
  import { timelineView } from '../state/timelineView.svelte';
  import { splitOps, splitTargets } from '../timeline/edits';
  import { buildLanes } from '../timeline/lanes';
  import { sliderToZoom, zoomToSlider } from '../timeline/scale';
  import Timeline from '../timeline/Timeline.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Slider from '../ui/Slider.svelte';

  // The timeline region: the toolbar (ux-spec #timeline) and the timeline itself (lib/timeline). The editing tools
  // that need history or the preview (undo, redo, split, delete) are placeholders until their cards.
  const zoomValue = $derived(
    zoomToSlider(timelineView.pxPerSecond, timelineView.duration, timelineView.width),
  );

  /** Cut the selected clips (or, with none selected, the topmost one) at the playhead: one edit, then select the right halves. */
  async function splitAtPlayhead(): Promise<void> {
    const time = playhead.time;
    const targets = splitTargets(buildLanes(project.timeline), time, clipSelection.ids);
    if (targets.length === 0) {
      toasts.show({
        kind: 'warning',
        message: clipSelection.ids.length
          ? 'The playhead is not inside a selected clip, so nothing was split.'
          : 'The playhead is not on a clip. Move it onto one, then split.',
      });
      return;
    }
    if (!(await project.applyOps(splitOps(targets, time)))) {
      reportEditError();
      return;
    }
    const clips = buildLanes(project.timeline).flatMap((l) => l.clips);
    const right = targets.flatMap((cut) => {
      const left = clips.find((c) => c.id === cut.id);
      return clips.filter(
        (c) =>
          c.id !== cut.id &&
          c.in_point === left?.out_point &&
          Math.abs(c.start - (left?.end ?? NaN)) < 1e-6,
      );
    });
    clipSelection.ids = right.map((c) => c.id);
    announce(`Split at ${time.toFixed(1)} seconds`);
  }
  $effect(() => registerAction('timeline.split', () => void splitAtPlayhead()));
  $effect(() => registerAction('timeline.splitAlias', () => void splitAtPlayhead()));
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
      disabled={!project.timeline?.tracks.some((t) => t.clips.length)}
      onclick={splitAtPlayhead}
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
