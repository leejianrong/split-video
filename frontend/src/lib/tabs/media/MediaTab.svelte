<script lang="ts">
  import { hasFileDrag } from '../../state/dnd';
  import { assets } from '../../state/assets.svelte';
  import { project } from '../../state/project.svelte';
  import { announce } from '../../a11y/announce';
  import Button from '../../ui/Button.svelte';
  import Icon from '../../ui/icons/Icon.svelte';
  import IconButton from '../../ui/IconButton.svelte';
  import AssetCard from './AssetCard.svelte';
  import PickDialog from './PickDialog.svelte';
  import QuickActions from './QuickActions.svelte';
  import UploadCard from './UploadCard.svelte';

  // The Media tab (ux-spec #left-tabs): import, the library as a grid of cards, and the quick actions. Dropping files
  // from the computer anywhere on the panel imports them; the Import button does the same with a file picker.
  let input: HTMLInputElement | undefined = $state();
  let pickOpen = $state(false);
  let dragging = $state(false);
  let depth = 0; // dragenter/dragleave fire for every child; count them so the overlay does not flicker

  // The open recording is the main clip, so it is always in the library; the rest comes from the asset table.
  $effect(() => {
    if (assets.status === 'idle') void assets.load();
  });
  $effect(() => {
    if (assets.status === 'ready' && project.id) void assets.ensure(project.id);
  });

  const empty = $derived(
    assets.status === 'ready' && assets.items.length === 0 && assets.uploads.length === 0,
  );

  function choose(event: Event) {
    const target = event.currentTarget as HTMLInputElement;
    if (target.files?.length) assets.enqueue(Array.from(target.files));
    target.value = ''; // the same file can be chosen again
  }

  function onDragEnter(event: DragEvent) {
    if (!hasFileDrag(event.dataTransfer)) return;
    event.preventDefault();
    depth += 1;
    dragging = true;
  }
  function onDragOver(event: DragEvent) {
    if (!hasFileDrag(event.dataTransfer)) return;
    event.preventDefault(); // accept the drop
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  }
  function onDragLeave() {
    depth = Math.max(0, depth - 1);
    if (depth === 0) dragging = false;
  }
  function onDrop(event: DragEvent) {
    if (!hasFileDrag(event.dataTransfer)) return;
    event.preventDefault();
    depth = 0;
    dragging = false;
    const files = Array.from(event.dataTransfer?.files ?? []);
    if (files.length) {
      assets.enqueue(files);
      announce(`Importing ${files.length === 1 ? files[0]?.name : `${files.length} files`}`);
    }
  }

  // Arrow keys move between the cards (two columns), as ux-spec #accessibility asks for lists.
  function onGridKeydown(event: KeyboardEvent) {
    const keys: Record<string, number> = {
      ArrowLeft: -1,
      ArrowRight: 1,
      ArrowUp: -2,
      ArrowDown: 2,
    };
    const step = keys[event.key];
    const current = (event.target as HTMLElement).closest('[data-testid="media-card"]');
    if (step === undefined || !current) return;
    const cards = Array.from(
      (event.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(
        '[data-testid="media-card"]',
      ),
    );
    const next = cards[cards.indexOf(current as HTMLElement) + step];
    if (!next) return;
    event.preventDefault();
    next.querySelector<HTMLElement>('button.select')?.focus();
  }
</script>

<!-- A drop target is not a control: the keyboard way to import is the Import button. -->
<section
  class="media"
  class:dragging
  aria-label="Media"
  data-testid="media-tab"
  ondragenter={onDragEnter}
  ondragover={onDragOver}
  ondragleave={onDragLeave}
  ondrop={onDrop}
>
  <header class="head">
    <h2>Media</h2>
    <span class="tools">
      <IconButton
        label="Add from your recordings folder"
        icon="folder"
        variant="secondary"
        onclick={() => (pickOpen = true)}
      />
      <Button variant="primary" data-testid="import" onclick={() => input?.click()}>
        <Icon name="plus" size={16} /> Import
      </Button>
    </span>
    <input
      bind:this={input}
      class="file-input"
      type="file"
      multiple
      accept="video/*,audio/*,.mkv,.ts,.m2ts"
      tabindex="-1"
      aria-hidden="true"
      data-testid="import-input"
      onchange={choose}
    />
  </header>

  <QuickActions />

  {#if assets.status === 'error' && assets.error}
    <div class="notice" role="alert" data-testid="library-error">
      <p>{assets.error.message}</p>
      {#if assets.error.fix}<p class="fix">{assets.error.fix}</p>{/if}
      <Button size="sm" onclick={() => assets.load()}>Try again</Button>
    </div>
  {:else if assets.status !== 'ready' && assets.items.length === 0}
    <p class="muted" role="status" data-testid="library-loading">Loading your library…</p>
  {:else if empty}
    <div class="empty" data-testid="panel-empty">
      <p class="lead">Add your first recording</p>
      <p class="muted">Drop a video here, or press Import. Everything stays on this computer.</p>
    </div>
  {:else}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <ul class="grid" aria-label="Library" data-testid="media-grid" onkeydown={onGridKeydown}>
      {#each assets.uploads as upload (upload.id)}
        <UploadCard {upload} />
      {/each}
      {#each assets.items as asset (asset.id)}
        <AssetCard {asset} />
      {/each}
    </ul>
  {/if}

  {#if dragging}
    <div class="drop" aria-hidden="true" data-testid="drop-overlay">
      <Icon name="upload" size={28} />
      <span>Drop to import</span>
    </div>
  {/if}
</section>

<PickDialog bind:open={pickOpen} />

<style>
  .media {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-height: 100%;
    padding: var(--space-4);
  }

  .head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  h2 {
    margin: 0;
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-bold);
  }

  .tools {
    display: inline-flex;
    gap: var(--space-2);
    margin-left: auto;
  }

  .file-input {
    display: none;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .empty,
  .notice {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
    margin: var(--space-5) 0;
    text-align: center;
  }

  .notice {
    align-items: stretch;
    padding: var(--space-3);
    border-left: 3px solid var(--color-danger);
    background: var(--color-surface-raised);
    text-align: left;
  }

  p {
    margin: 0;
  }

  .lead {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
  }

  .muted,
  .fix {
    color: var(--color-text-muted);
  }

  .drop {
    position: absolute;
    inset: var(--space-2);
    z-index: var(--z-sticky);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    border: 2px dashed var(--color-accent);
    border-radius: var(--radius-lg);
    background: var(--color-surface);
    color: var(--color-accent);
    pointer-events: none;
  }
</style>
