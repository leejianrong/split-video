<script lang="ts">
  import type { Snippet } from 'svelte';
  import { detectMac } from '../a11y/dispatcher';
  import { displayKeys, findShortcut } from '../a11y/shortcuts';

  // A visual hint on hover and keyboard focus: the label and, when `action` names a registry entry, its keys.
  // The tooltip is aria-hidden: the wrapped control must carry its own accessible name (IconButton does) and
  // `aria-keyshortcuts` where it matters, so a screen reader does not hear the label twice.
  let { label, action, children }: { label: string; action?: string; children: Snippet } = $props();

  const keys = $derived.by(() => {
    const entry = action ? findShortcut(action) : undefined;
    return entry ? displayKeys(entry, detectMac()).join(' / ') : '';
  });
</script>

<span class="tip">
  {@render children()}
  <span class="bubble" aria-hidden="true">
    {label}
    {#if keys}<kbd>{keys}</kbd>{/if}
  </span>
</span>

<style>
  .tip {
    position: relative;
    display: inline-flex;
  }

  .bubble {
    position: absolute;
    top: calc(100% + var(--space-2));
    left: 50%;
    transform: translateX(-50%);
    z-index: var(--z-tooltip);
    display: inline-flex;
    gap: var(--space-2);
    align-items: center;
    padding: var(--space-1) var(--space-2);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-sm);
    background: var(--color-surface-raised);
    color: var(--color-text);
    font-size: var(--font-size-xs);
    white-space: nowrap;
    box-shadow: var(--shadow-popover);
    pointer-events: none;
    opacity: 0;
    visibility: hidden;
    transition:
      opacity var(--duration-fast),
      visibility var(--duration-fast);
  }

  .tip:hover .bubble,
  .tip:focus-within .bubble {
    opacity: 1;
    visibility: visible;
    transition-delay: 400ms;
  }
</style>
