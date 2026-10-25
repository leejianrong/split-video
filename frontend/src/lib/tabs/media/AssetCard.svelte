<script lang="ts">
  import { formatDuration } from '../../format';
  import { baseName } from '../../format';
  import { assets, posterUrl } from '../../state/assets.svelte';
  import { setAssetDrag } from '../../state/dnd';
  import { project } from '../../state/project.svelte';
  import { selection } from '../../state/selection.svelte';
  import type { AssetOut } from '../../api/client';
  import { toasts } from '../../state/toasts.svelte';
  import { announce } from '../../a11y/announce';
  import Icon from '../../ui/icons/Icon.svelte';
  import Button from '../../ui/Button.svelte';
  import ProgressBar from '../../ui/ProgressBar.svelte';

  // One library asset: thumbnail, name and length. Click or Space selects; double click, Enter or the add button puts
  // it on the timeline; dragging it onto the timeline does the same (the payload is documented in state/dnd.ts).
  let { asset }: { asset: AssetOut } = $props();

  const name = $derived(baseName(asset.path));
  const selected = $derived(selection.assetId === asset.id);
  const card = $derived(assets.cardOf(asset));
  const poster = $derived(posterUrl(asset));
  let posterFailed = $state(false);
  let adding = $state(false);
  let locating = $state(false);

  const canAdd = $derived(asset.present && asset.has_video && !adding);
  const addReason = $derived(
    !asset.present
      ? 'The file is missing'
      : !asset.has_video
        ? 'Audio goes on a music lane, from the Audio tab (coming later)'
        : '',
  );

  async function add() {
    if (!canAdd) return;
    adding = true;
    const ok = await project.addAsset(asset.id);
    adding = false;
    if (ok) {
      announce(`Added ${name} to the timeline`);
    } else if (project.editError) {
      toasts.error(`${project.editError.message} ${project.editError.fix}`.trim());
    }
  }

  async function locate() {
    locating = true;
    const found = await assets.locate(asset.id);
    locating = false;
    if (!found) {
      toasts.error(
        `${name} was not found. Fix: move it back into the recordings folder, or import it again.`,
      );
    }
  }

  function onKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      void add();
    }
  }

  function onDragStart(event: DragEvent) {
    if (!event.dataTransfer || !canAdd) {
      event.preventDefault();
      return;
    }
    setAssetDrag(event.dataTransfer, { id: asset.id, name, duration: asset.duration });
    selection.select(asset.id);
  }
</script>

<li class="cell" data-testid="media-card" data-asset={asset.id} data-state={card.kind}>
  <div class="card" class:selected>
    <button
      type="button"
      class="select"
      aria-pressed={selected}
      aria-label={`${name}, ${formatDuration(asset.duration)}${card.kind === 'missing' ? ', file not found' : ''}`}
      draggable={canAdd}
      ondragstart={onDragStart}
      onclick={() => selection.select(asset.id)}
      ondblclick={add}
      onkeydown={onKeydown}
    >
      <span class="thumb">
        {#if poster && !posterFailed}
          <img
            src={poster}
            alt=""
            width="240"
            loading="lazy"
            decoding="async"
            draggable="false"
            onerror={() => (posterFailed = true)}
          />
        {:else}
          <Icon name={asset.has_video ? 'video' : 'audio'} size={28} />
        {/if}
        <span class="dur">{formatDuration(asset.duration)}</span>
        {#if card.kind === 'missing'}
          <span class="badge danger"><Icon name="alert" size={12} /> File not found</span>
        {/if}
      </span>
      <span class="cap" title={asset.path}>{name}</span>
    </button>

    {#if card.preparing !== null}
      <div class="status">
        <span>Preparing preview {card.preparing}%</span>
        <ProgressBar value={card.preparing / 100} label={`Preparing preview of ${name}`} />
      </div>
    {/if}

    {#if card.kind === 'missing'}
      <div class="status">
        <Button size="sm" onclick={locate} disabled={locating}>
          {locating ? 'Looking…' : 'Locate'}
        </Button>
      </div>
    {:else}
      <span class="add">
        <!-- A native title, not a Tooltip: a bubble at the card's edge would widen the panel's scroll area. -->
        <Button
          iconOnly
          size="sm"
          variant="secondary"
          aria-label={`Add ${name} to the timeline`}
          title={canAdd ? 'Add to timeline' : `Cannot add: ${addReason}`}
          disabled={!canAdd}
          onclick={add}
        >
          <Icon name="plus" size={16} />
        </Button>
      </span>
    {/if}
  </div>
</li>

<style>
  .cell {
    position: relative;
    min-width: 0;
    list-style: none;
  }

  .card {
    position: relative;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    background: var(--color-surface-raised);
  }

  .card.selected {
    border-color: var(--color-accent);
    box-shadow: 0 0 0 1px var(--color-accent);
  }

  .select {
    display: flex;
    flex-direction: column;
    width: 100%;
    padding: 0;
    border: 0;
    background: none;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .thumb {
    position: relative;
    display: grid;
    place-items: center;
    height: 78px;
    overflow: hidden;
    background: var(--color-bg);
    color: var(--color-text-muted);
  }

  .thumb img {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .dur {
    position: absolute;
    right: var(--space-1);
    bottom: var(--space-1);
    padding: 1px var(--space-1);
    border-radius: var(--radius-sm);
    background: var(--color-bg);
    color: var(--color-text);
    font-size: var(--font-size-xs);
  }

  .badge {
    position: absolute;
    left: var(--space-1);
    top: var(--space-1);
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 1px var(--space-1);
    border-radius: var(--radius-sm);
    background: var(--color-bg);
    font-size: var(--font-size-xs);
  }

  .badge.danger {
    color: var(--color-danger);
  }

  .cap {
    padding: var(--space-1) var(--space-2);
    overflow: hidden;
    font-size: var(--font-size-xs);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .status {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding: 0 var(--space-2) var(--space-2);
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
  }

  .add {
    position: absolute;
    top: var(--space-1);
    right: var(--space-1);
    opacity: 0;
  }

  .card:hover .add,
  .card:focus-within .add,
  .card.selected .add {
    opacity: 1;
  }

  @media (prefers-reduced-motion: no-preference) {
    .add {
      transition: opacity var(--duration-fast);
    }
  }
</style>
