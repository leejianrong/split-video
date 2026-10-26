<script lang="ts">
  import { assets, type Upload } from '../../state/assets.svelte';
  import Button from '../../ui/Button.svelte';
  import Icon from '../../ui/icons/Icon.svelte';
  import ProgressBar from '../../ui/ProgressBar.svelte';

  // A file on its way in: importing (the bytes are being copied into the recordings folder, with a percent) or
  // failed (what went wrong, one thing to try, Retry). It becomes an AssetCard when the server has registered it.
  let { upload }: { upload: Upload } = $props();

  const fraction = $derived(upload.size > 0 ? upload.loaded / upload.size : 0);
  const percent = $derived(Math.round(fraction * 100));
</script>

<li
  class="cell"
  data-testid="upload-card"
  data-state={upload.status === 'failed' ? 'failed' : 'importing'}
>
  <div class="card" class:failed={upload.status === 'failed'}>
    <span class="thumb">
      <Icon name={upload.status === 'failed' ? 'alert' : 'upload'} size={26} />
    </span>
    <span class="cap" title={upload.name}>{upload.name}</span>
    <div class="status">
      {#if upload.status === 'failed' && upload.error}
        <span class="badge"><Icon name="alert" size={12} /> Import failed</span>
        <p class="msg" data-testid="upload-error" data-code={upload.error.code}>
          {upload.error.message}
        </p>
        {#if upload.error.fix}<p class="fix">Fix: {upload.error.fix}</p>{/if}
        <span class="actions">
          <Button size="sm" onclick={() => assets.retry(upload.id)}>Retry</Button>
          <Button size="sm" variant="ghost" onclick={() => assets.cancelUpload(upload.id)}>
            Dismiss
          </Button>
        </span>
      {:else}
        <span>
          {upload.status === 'queued' ? 'Waiting to import' : `Importing ${percent}%`}
        </span>
        <ProgressBar
          value={upload.status === 'queued' ? 0 : fraction}
          label={`Importing ${upload.name}`}
        />
      {/if}
    </div>
    {#if upload.status !== 'failed'}
      <span class="cancel">
        <Button
          iconOnly
          size="sm"
          variant="secondary"
          aria-label={`Cancel importing ${upload.name}`}
          title="Cancel"
          onclick={() => assets.cancelUpload(upload.id)}
        >
          <Icon name="close" size={16} />
        </Button>
      </span>
    {/if}
  </div>
</li>

<style>
  .cell {
    min-width: 0;
    list-style: none;
  }

  .card {
    position: relative;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    border: 1px dashed var(--color-border-strong);
    border-radius: var(--radius-md);
    background: var(--color-surface-raised);
  }

  .card.failed {
    border-style: solid;
    border-color: var(--color-danger);
  }

  .thumb {
    display: grid;
    place-items: center;
    height: 78px;
    background: var(--color-bg);
    color: var(--color-text-muted);
  }

  .failed .thumb {
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

  .badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    color: var(--color-danger);
  }

  .msg,
  .fix {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .msg {
    color: var(--color-text);
  }

  .actions {
    display: flex;
    gap: var(--space-1);
    margin-top: var(--space-1);
  }

  .cancel {
    position: absolute;
    top: var(--space-1);
    right: var(--space-1);
  }
</style>
