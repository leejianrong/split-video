<script lang="ts">
  import type { Snippet } from 'svelte';

  // A titled frame: the container of a left-tab section or an inspector group. `level` is the heading level.
  let {
    title,
    level = 2,
    actions,
    children,
  }: { title: string; level?: 2 | 3 | 4; actions?: Snippet; children: Snippet } = $props();

  const uid = $props.id();
  const titleId = $derived(`panel-${uid}`);
</script>

<section class="panel" aria-labelledby={titleId}>
  <header>
    <svelte:element this={`h${level}`} id={titleId}>{title}</svelte:element>
    {#if actions}<div class="actions">{@render actions()}</div>{/if}
  </header>
  <div class="body">{@render children()}</div>
</section>

<style>
  .panel {
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    background: var(--color-surface);
    overflow: hidden;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    min-height: var(--control-height-lg);
    padding: var(--space-1) var(--space-3);
    border-bottom: 1px solid var(--color-border);
    background: var(--color-surface-raised);
  }

  header :global(:is(h2, h3, h4)) {
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-bold);
  }

  .actions {
    display: flex;
    gap: var(--space-2);
  }

  .body {
    padding: var(--space-3);
  }
</style>
