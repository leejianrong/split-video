<script lang="ts">
  import { ApiError, browseApiV1BrowseGet, type BrowseResponse } from '../../api/client';
  import { assets } from '../../state/assets.svelte';
  import { toasts } from '../../state/toasts.svelte';
  import { announce } from '../../a11y/announce';
  import Button from '../../ui/Button.svelte';
  import Dialog from '../../ui/Dialog.svelte';
  import Icon from '../../ui/icons/Icon.svelte';

  // Add files that are already in the recordings folder to the library, without copying them. The drop and the
  // Import button copy a file from anywhere on the computer in (a browser cannot give the server a path); this is
  // the way to use a very large recording where it already is.
  let { open = $bindable(false) }: { open: boolean } = $props();

  let dir = $state('');
  let listing = $state<BrowseResponse | null>(null);
  let loading = $state(false);
  let failure = $state<string | null>(null);
  let adding = $state<string | null>(null);

  async function load(path: string) {
    loading = true;
    failure = null;
    try {
      const { data } = await browseApiV1BrowseGet({ query: { path } });
      listing = data;
      dir = data.cwd;
    } catch (cause) {
      failure =
        cause instanceof ApiError
          ? `${cause.message} ${cause.fix}`.trim()
          : 'The folder could not be listed.';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    if (open) void load(dir);
  });

  const inLibrary = (path: string) => assets.byPath(path) !== undefined;

  async function add(path: string, name: string) {
    adding = path;
    try {
      await assets.register(path);
      announce(`Added ${name} to the library`);
    } catch (cause) {
      const text =
        cause instanceof ApiError
          ? `${cause.message} ${cause.fix}`.trim()
          : 'It could not be added.';
      toasts.error(`${name}: ${text}`);
    } finally {
      adding = null;
    }
  }
</script>

<Dialog
  bind:open
  title="Add from your recordings folder"
  description="These files stay where they are."
>
  <div class="browser" data-testid="pick-dialog">
    <p class="where">
      <Icon name="folder" size={14} />
      <span>{listing?.cwd ? `/${listing.cwd}` : 'Recordings folder'}</span>
    </p>
    {#if failure}
      <p class="error" role="alert">{failure}</p>
    {:else if loading && !listing}
      <p class="muted" role="status">Loading…</p>
    {:else if listing}
      <ul class="entries">
        {#if listing.parent !== null}
          <li>
            <button type="button" class="row" onclick={() => load(listing?.parent ?? '')}>
              <Icon name="arrowUp" size={14} /> <span>Up one folder</span>
            </button>
          </li>
        {/if}
        {#each listing.entries as entry (entry.path)}
          <li>
            {#if entry.is_dir}
              <button type="button" class="row" onclick={() => load(entry.path)}>
                <Icon name="folder" size={14} /> <span>{entry.name}</span>
              </button>
            {:else}
              <div class="row file">
                <Icon name="video" size={14} />
                <span class="file-name" title={entry.path}>{entry.name}</span>
                {#if inLibrary(entry.path)}
                  <span class="muted">In the library</span>
                {:else}
                  <Button
                    size="sm"
                    disabled={adding !== null}
                    aria-label={`Add ${entry.name} to the library`}
                    onclick={() => add(entry.path, entry.name)}
                  >
                    {adding === entry.path ? 'Adding…' : 'Add'}
                  </Button>
                {/if}
              </div>
            {/if}
          </li>
        {:else}
          <li class="muted pad">No videos or audio files in this folder.</li>
        {/each}
      </ul>
    {/if}
  </div>
  {#snippet footer()}
    <Button onclick={() => (open = false)}>Done</Button>
  {/snippet}
</Dialog>

<style>
  .browser {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 360px;
  }

  .where,
  .error,
  .muted {
    margin: 0;
    color: var(--color-text-muted);
  }

  .where {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .error {
    color: var(--color-danger);
  }

  .entries {
    display: flex;
    flex-direction: column;
    max-height: 320px;
    margin: 0;
    padding: 0;
    overflow: auto;
    list-style: none;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
  }

  .row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    min-height: 36px;
    padding: var(--space-1) var(--space-3);
    border: 0;
    background: none;
    color: inherit;
    font: inherit;
    text-align: left;
  }

  button.row {
    cursor: pointer;
  }

  button.row:hover {
    background: var(--color-surface-hover);
  }

  .file-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pad {
    padding: var(--space-3);
  }
</style>
