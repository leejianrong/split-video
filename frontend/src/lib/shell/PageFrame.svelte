<script lang="ts">
  import type { Snippet } from 'svelte';
  import { hrefTo } from '../state/route';
  import ServerStatus from './ServerStatus.svelte';

  // The frame of the pages that are not the editor (start, library, project errors): a top bar and a main region.
  let { title, note, children }: { title: string; note?: string; children: Snippet } = $props();
</script>

<div class="page">
  <header class="bar">
    <a class="logo" href={hrefTo({ view: 'start' })}>split-video</a>
    <span class="title">{title}</span>
    {#if note}<span class="note">{note}</span>{/if}
    <ServerStatus />
  </header>
  <main class="main">{@render children()}</main>
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    min-height: 100vh;
  }

  .bar {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    height: var(--size-topbar-height);
    padding: 0 var(--space-4);
    border-bottom: 1px solid var(--color-border);
    background: var(--color-surface);
  }

  .logo {
    color: var(--color-text);
    font-weight: var(--font-weight-bold);
    font-size: var(--font-size-md);
    text-decoration: none;
  }

  .title,
  .note {
    color: var(--color-text-muted);
  }

  .main {
    flex: 1;
    min-width: 0;
    padding: var(--space-6) var(--space-7);
  }
</style>
