<script lang="ts">
  import { formatClock } from '../format';
  import { project } from '../state/project.svelte';
  import Button from '../ui/Button.svelte';
  import Icon from '../ui/icons/Icon.svelte';
  import IconButton from '../ui/IconButton.svelte';

  // The preview region: a header with the time readout, the canvas frame in the project's ratio, and the
  // transport. The picture and playback arrive with the preview cards, so the controls are disabled until then.
  const total = $derived(project.project ? formatClock(project.project.duration) : '0:00:00');
</script>

<main class="preview" data-region="preview" aria-label="Preview">
  <div class="header">
    <h2>Preview</h2>
    <span class="readout" data-testid="readout">0:00:00 / {total}</span>
    <Button size="sm" disabled aria-label="Fit">Fit <Icon name="chevronDown" size={14} /></Button>
  </div>
  <div class="stage">
    <div class="frame" data-testid="preview-frame">
      <span class="ratio">16:9</span>
      <p>Your timeline shows here.</p>
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
    width: min(100cqw, calc(100cqh * 16 / 9));
    aspect-ratio: 16 / 9;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-sm);
    background: var(--color-surface);
    color: var(--color-text-muted);
  }

  .frame p {
    margin: 0;
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
