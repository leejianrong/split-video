<script lang="ts">
  import { browseApiV1BrowseGet, type BrowseResponse } from '../api/client';
  import { router } from '../state/route.svelte';
  import { hrefTo } from '../state/route';
  import { toProjectError, type ProjectError } from '../state/project.svelte';
  import PageFrame from '../shell/PageFrame.svelte';
  import Button from '../ui/Button.svelte';
  import Icon from '../ui/icons/Icon.svelte';

  // The library: the folders and videos under the server's root, from GET /api/v1/browse. Opening a video goes to
  // #/p/<path>, which opens it in the editor.
  type State =
    | { kind: 'loading' }
    | { kind: 'ready'; listing: BrowseResponse }
    | { kind: 'error'; error: ProjectError };

  const dir = $derived(router.current.view === 'library' ? router.current.dir : '');
  let state = $state<State>({ kind: 'loading' });
  let ticket = 0;

  async function load(path: string) {
    const mine = ++ticket;
    state = { kind: 'loading' };
    try {
      const { data } = await browseApiV1BrowseGet({ query: { path } });
      if (mine === ticket) state = { kind: 'ready', listing: data };
    } catch (cause) {
      if (mine === ticket) state = { kind: 'error', error: toProjectError(cause) };
    }
  }

  $effect(() => {
    void load(dir);
  });
</script>

<PageFrame title="Library">
  <div class="library">
    <div class="head">
      <h1>Your recordings</h1>
      {#if state.kind === 'ready'}
        <span class="where" data-testid="library-path">{state.listing.cwd || 'All recordings'}</span
        >
        {#if state.listing.parent !== null}
          <a class="up" href={hrefTo({ view: 'library', dir: state.listing.parent })}>
            <Icon name="arrowUp" size={16} /> Up one folder
          </a>
        {/if}
      {/if}
    </div>

    {#if state.kind === 'loading'}
      <p class="status" role="status" data-testid="library-loading">Loading your recordings…</p>
    {:else if state.kind === 'error'}
      <div class="status error" role="alert" data-testid="library-error">
        <p><strong>Could not load the library.</strong> {state.error.message}</p>
        {#if state.error.fix}<p class="fix">{state.error.fix}</p>{/if}
        <Button onclick={() => load(dir)}>Try again</Button>
      </div>
    {:else if state.listing.entries.length === 0}
      <div class="status" data-testid="library-empty">
        <p><strong>No videos here yet.</strong></p>
        <p class="fix">
          Put a recording in the folder you started the editor on, then reload this page.
        </p>
      </div>
    {:else}
      <ul class="entries" data-testid="library-list">
        {#each state.listing.entries as entry (entry.path)}
          <li>
            <a
              class="entry"
              href={entry.is_dir
                ? hrefTo({ view: 'library', dir: entry.path })
                : hrefTo({ view: 'editor', id: entry.path })}
              data-testid={entry.is_dir ? 'library-folder' : 'library-video'}
            >
              <Icon name={entry.is_dir ? 'folder' : 'video'} size={20} />
              <span class="name">{entry.name}</span>
              <span class="action">{entry.is_dir ? 'Open folder' : 'Open in the editor'}</span>
            </a>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</PageFrame>

<style>
  .library {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    max-width: 840px;
    margin: 0 auto;
  }

  .head {
    display: flex;
    align-items: baseline;
    gap: var(--space-4);
  }

  h1 {
    font-size: var(--font-size-xl);
  }

  .where {
    color: var(--color-text-muted);
    overflow-wrap: anywhere;
  }

  .up {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    color: var(--color-accent);
  }

  .entries {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .entry {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    background: var(--color-surface);
    color: var(--color-text);
    text-decoration: none;
  }
  .entry:hover {
    background: var(--color-surface-hover);
  }

  .name {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .action {
    color: var(--color-text-muted);
    font-size: var(--font-size-sm);
  }

  .status {
    padding: var(--space-6);
    border: 1px dashed var(--color-border-strong);
    border-radius: var(--radius-lg);
    text-align: center;
    color: var(--color-text-muted);
  }
  .status p {
    margin: 0 0 var(--space-2);
  }
  .status strong {
    color: var(--color-text);
  }
  .fix {
    color: var(--color-text-muted);
  }
</style>
