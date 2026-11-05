<script lang="ts">
  import { playback } from '../state/playback.svelte';
  import { draftFor } from '../state/draft';
  import { project } from '../state/project.svelte';
  import { videoSlot } from '../state/videoSlot';

  // The three <video> elements playback drives, stacked over the still: two for the composite chunks (double buffer:
  // the one playing and the next, loaded and paused) and one for the draft main-track file shown while a chunk renders.
  // Hidden ones stay in the DOM (visibility, not display) so a preloaded first frame is ready at the swap.
  let a = $state<HTMLVideoElement>();
  let b = $state<HTMLVideoElement>();
  let draft = $state<HTMLVideoElement>();

  $effect(() => {
    if (!a || !b || !draft) return;
    playback.attach([videoSlot(a), videoSlot(b)], videoSlot(draft), (t) =>
      draftFor(project.timeline, t),
    );
    return () => playback.dispose();
  });
</script>

<video
  bind:this={a}
  data-testid="preview-video"
  data-slot="0"
  class:on={playback.activeSlot === 0}
  playsinline
  aria-hidden="true"
></video>
<video
  bind:this={b}
  data-testid="preview-video"
  data-slot="1"
  class:on={playback.activeSlot === 1}
  playsinline
  aria-hidden="true"
></video>
<video
  bind:this={draft}
  data-testid="preview-draft"
  class:on={playback.showing === 'draft'}
  playsinline
  aria-hidden="true"
></video>
{#if playback.showing === 'draft'}
  <span class="badge" role="status" data-testid="preview-draft-badge">
    Draft preview, overlays loading
  </span>
{:else if playback.state === 'buffering' && playback.showing === 'still'}
  <span class="badge" role="status" data-testid="preview-preparing">Preparing preview</span>
{/if}
{#if playback.message}
  <p class="problem" role="status" data-testid="playback-message">{playback.message}</p>
{/if}

<style>
  video {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: var(--color-surface);
    visibility: hidden;
    pointer-events: none;
  }

  video.on {
    visibility: visible;
  }

  .badge {
    position: absolute;
    left: var(--space-2);
    bottom: var(--space-2);
    padding: 0 var(--space-2);
    border-radius: var(--radius-sm);
    background: var(--color-surface-raised);
    color: var(--color-text);
    font-size: var(--font-size-xs);
    pointer-events: none;
  }

  .problem {
    position: absolute;
    left: var(--space-2);
    right: var(--space-2);
    top: var(--space-2);
    margin: 0;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-sm);
    background: var(--color-surface-raised);
    color: var(--color-text);
    font-size: var(--font-size-xs);
  }
</style>
