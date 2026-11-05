<script lang="ts">
  import { formatClock } from '../format';
  import { playhead } from '../state/playhead.svelte';
  import { preview } from '../state/preview.svelte';
  import { project } from '../state/project.svelte';
  import Button from '../ui/Button.svelte';
  import Icon from '../ui/icons/Icon.svelte';
  import IconButton from '../ui/IconButton.svelte';

  // The preview region: a header with the time readout, the canvas frame in the project's ratio holding the still of
  // the timeline at the playhead, and the transport. Playback arrives with its own card, so the controls stay disabled.
  const total = $derived(project.project ? formatClock(project.project.duration) : '0:00:00');
  const hasClips = $derived(project.timeline?.tracks.some((t) => t.clips.length > 0) ?? false);

  // Ask for a picture as wide as the frame is on screen, rounded up to 64 px so dragging a panel edge does not send a
  // request per pixel; the server never goes above the export's own width.
  let frameWidth = $state(0);
  const stillWidth = $derived(
    frameWidth > 0
      ? Math.min(
          1280,
          Math.ceil((frameWidth * Math.min(window.devicePixelRatio || 1, 2)) / 64) * 64,
        )
      : 0,
  );

  $effect(() => {
    const path = project.id;
    if (path === null || !hasClips || stillWidth === 0) {
      preview.clear();
      return;
    }
    preview.request({ path, time: playhead.time, width: stillWidth, revision: project.revision });
  });
  $effect(() => () => preview.clear());
</script>

<main class="preview" data-region="preview" aria-label="Preview">
  <div class="header">
    <h2>Preview</h2>
    <span class="readout" data-testid="readout">{formatClock(playhead.time)} / {total}</span>
    <Button size="sm" disabled aria-label="Fit">Fit <Icon name="chevronDown" size={14} /></Button>
  </div>
  <div class="stage">
    <div
      class="frame"
      data-testid="preview-frame"
      style:--ratio={preview.ratio}
      bind:clientWidth={frameWidth}
    >
      {#if preview.url}
        <img
          src={preview.url}
          alt="The timeline at {formatClock(preview.time ?? playhead.time)}"
          data-testid="preview-still"
          data-time={preview.time}
          draggable="false"
        />
      {:else}
        <span class="ratio">16:9</span>
        <p>Your timeline shows here.</p>
      {/if}
      {#if preview.busy && preview.url}
        <span class="spinner" role="status" aria-label="Updating the preview"></span>
      {/if}
      {#if preview.error}
        <p class="problem" role="status" data-testid="preview-error">
          {preview.error.message}
          {preview.error.fix}
        </p>
      {/if}
    </div>
  </div>
  <div class="transport" role="group" aria-label="Playback">
    <IconButton label="Previous cut" icon="skipBack" action="transport.cut" disabled />
    <IconButton label="Play / pause" icon="play" action="transport.toggle" size="lg" disabled />
    <IconButton label="Next cut" icon="skipForward" action="transport.cut" disabled />
  </div>
</main>

<style>
  .preview {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    background: var(--color-bg);
  }

  .header {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    height: 36px;
    padding: 0 var(--space-4);
    color: var(--color-text-muted);
    font-size: var(--font-size-sm);
  }

  h2 {
    margin-right: auto;
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--color-text-muted);
  }

  .readout {
    font-variant-numeric: tabular-nums;
  }

  /* The frame is the largest 16:9 box that fits the stage; it is a container so cq units can size it. */
  .stage {
    flex: 1;
    min-height: 0;
    display: grid;
    place-items: center;
    padding: var(--space-4);
    container-type: size;
  }

  .frame {
    position: relative;
    display: grid;
    place-items: center;
    width: min(100cqw, calc(100cqh * var(--ratio, 1.7778)));
    aspect-ratio: var(--ratio, 1.7778);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-sm);
    background: var(--color-surface);
    color: var(--color-text-muted);
  }

  .frame p {
    margin: 0;
  }

  .frame img {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
    user-select: none;
  }

  .spinner {
    position: absolute;
    top: var(--space-2);
    right: var(--space-2);
    width: 16px;
    height: 16px;
    border: 2px solid var(--color-border);
    border-top-color: var(--color-text);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .spinner {
      animation-duration: 3s;
    }
  }

  .problem {
    position: absolute;
    left: var(--space-2);
    right: var(--space-2);
    bottom: var(--space-2);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-sm);
    background: var(--color-surface-raised);
    color: var(--color-text);
    font-size: var(--font-size-xs);
  }

  .ratio {
    position: absolute;
    top: var(--space-2);
    left: var(--space-2);
    padding: 0 var(--space-2);
    border-radius: var(--radius-sm);
    background: var(--color-surface-raised);
    font-size: var(--font-size-xs);
  }

  .transport {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-4);
    height: 52px;
  }
</style>
