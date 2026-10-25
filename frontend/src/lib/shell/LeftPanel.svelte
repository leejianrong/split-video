<script lang="ts">
  import { formatLength } from '../format';
  import { project } from '../state/project.svelte';
  import type { TabId } from '../state/layout';
  import { EMPTY_COPY, TABS } from './tabs';
  import Icon from '../ui/icons/Icon.svelte';

  // The content of one left tab. Panel contents are built per tab in later cards; for now each tab says what its
  // empty state says (ux-spec #empty-states), and Media lists the open recording.
  let { tab }: { tab: TabId } = $props();
  const title = $derived(TABS.find((t) => t.id === tab)?.label ?? '');
</script>

<section class="panel-content" aria-label={title}>
  <h2>{title}</h2>
  {#if tab === 'media' && project.project}
    <ul class="cards">
      <li class="card" data-testid="media-card">
        <span class="thumb"><Icon name="video" size={24} /></span>
        <span class="meta">
          <span class="file">{project.project.filename}</span>
          <span class="length">{formatLength(project.project.duration)}</span>
        </span>
      </li>
    </ul>
  {:else}
    <p class="empty" data-testid="panel-empty">{EMPTY_COPY[tab]}</p>
  {/if}
</section>

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

  .cards {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .card {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    background: var(--color-surface-raised);
  }

  .thumb {
    display: grid;
    place-items: center;
    flex: none;
    width: 64px;
    height: 40px;
    border-radius: var(--radius-sm);
    background: var(--color-bg);
    color: var(--color-text-muted);
  }

  .meta {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .file {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .length {
    color: var(--color-text-muted);
    font-size: var(--font-size-sm);
  }
</style>
