<script lang="ts">
  import { registerAction } from '../a11y/dispatcher';
  import { formatClock } from '../format';
  import { playback } from '../state/playback.svelte';
  import { playhead } from '../state/playhead.svelte';
  import { canvasOf } from '../state/canvas';
  import { preview } from '../state/preview.svelte';
  import { project } from '../state/project.svelte';
  import Button from '../ui/Button.svelte';
  import Icon from '../ui/icons/Icon.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import PlaybackSurface from './PlaybackSurface.svelte';

  // The preview region: a header with the time readout, the canvas frame in the canvas ratio holding the still of
  // the timeline at the playhead (the still), the chunk videos that play over it (PlaybackSurface), and the transport.
  const total = $derived(project.project ? formatClock(project.project.duration) : '0:00:00');
  const hasClips = $derived(project.timeline?.tracks.some((t) => t.clips.length > 0) ?? false);

  const canvas = $derived(canvasOf(project.canvas));

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

  // The still is for a paused or waiting playhead; while a video is on screen the video is the picture.
  $effect(() => {
    const path = project.id;
    if (path === null || !hasClips || stillWidth === 0) {
      preview.clear();
      return;
    }
    if (playback.showing !== 'still' && playback.state !== 'paused') return;
    preview.request({
      path,
      time: playhead.time,
      width: stillWidth,
      preset: canvas.preset,
      revision: project.revision,
    });
  });
  $effect(() => () => preview.clear());

  // Paused on the last frame of a video: it stays until the still of that moment has arrived, then the still takes over.
  $effect(() => {
    if (
      playback.state === 'paused' &&
      playback.showing !== 'still' &&
      !preview.busy &&
      preview.time !== null &&
      Math.abs(preview.time - playhead.time) < 0.25
    )
      playback.release();
  });

  $effect(() => {
    playback.sync({
      path: hasClips ? project.id : null,
      revision: project.revision,
      preset: canvas.preset,
      duration: project.timeline?.duration ?? 0,
    });
  });
  $effect(() => playhead.onSeek((time) => playback.seeked(time)));
  $effect(() => registerAction('transport.toggle', () => playback.toggle()));
  $effect(() =>
    registerAction('transport.edge', ({ key }) => {
      playhead.seek(key === 'home' ? 0 : playhead.duration);
    }),
  );
  const playing = $derived(playback.state !== 'paused');
</script>

<main class="preview" data-region="preview" aria-label="Preview">
  <div class="header">
    <h2>Preview</h2>
    <span class="readout" data-testid="readout">{formatClock(playhead.time)} / {total}</span>
    <Button size="sm" disabled aria-label="Fit">Fit <Icon name="chevronDown" size={14} /></Button>
  </div>
  <div class="stage">
    <!-- Clicking the picture toggles playback; the keyboard has Space and K (transport.toggle), so no key handler here. -->
    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
    <div
      class="frame"
      data-testid="preview-frame"
      data-playback={playback.state}
      data-showing={playback.showing}
      style:--ratio={canvas.ratio}
      bind:clientWidth={frameWidth}
      onclick={() => hasClips && playback.toggle()}
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
        <span class="ratio">{project.canvas}</span>
        <p>Your timeline shows here.</p>
      {/if}
      <PlaybackSurface />
      {#if preview.busy && preview.url && !playing}
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
    <IconButton
      label={playing ? 'Pause' : 'Play'}
      icon={playing ? 'pause' : 'play'}
      action="transport.toggle"
      size="lg"
      disabled={!hasClips}
      aria-pressed={playing}
      onclick={() => playback.toggle()}
      data-testid="play"
    />
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

  /* The frame is the largest box in the canvas ratio that fits the stage; it is a container so cq units can size it. */
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
