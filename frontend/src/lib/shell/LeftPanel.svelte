<script lang="ts">
  import type { TabId } from '../state/layout';
  import MediaTab from '../tabs/media/MediaTab.svelte';
  import { EMPTY_COPY, TABS } from './tabs';

  // The content of one left tab. Media is built (SPL-63); the other tabs say what their empty state says
  // (ux-spec #empty-states) until their cards land.
  let { tab }: { tab: TabId } = $props();
  const title = $derived(TABS.find((t) => t.id === tab)?.label ?? '');
</script>

{#if tab === 'media'}
  <MediaTab />
{:else}
  <section class="panel-content" aria-label={title}>
    <h2>{title}</h2>
    <p class="empty" data-testid="panel-empty">{EMPTY_COPY[tab]}</p>
  </section>
{/if}

<style>
  .panel-content {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding: var(--space-4);
  }

  h2 {
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-bold);
  }

  .empty {
    margin: var(--space-5) 0;
    color: var(--color-text-muted);
    text-align: center;
  }
</style>
