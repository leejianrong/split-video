<script lang="ts">
  import { exports, isActive } from '../state/exports.svelte';
  import Button from '../ui/Button.svelte';
  import Icon from '../ui/icons/Icon.svelte';
  import ProgressBar from '../ui/ProgressBar.svelte';

  // The render queue (ux-spec #export): one row per output file with its state, a bar and a percent while it
  // renders, Cancel on a waiting or running file, and for a failed one what went wrong, "Fix: ..." and Retry.
  // One failed render never stops the others. The rows come from the job queue, so closing the dialog changes nothing.
  const rows = $derived(exports.rows);
  const percent = (p: number) => `${Math.round(Math.min(Math.max(p, 0), 1) * 100)}%`;
  const LABEL = {
    waiting: 'Waiting',
    rendering: 'Rendering',
    done: 'Done',
    failed: 'Failed',
    cancelled: 'Cancelled',
  } as const;
</script>

<section class="queue" aria-label="Render queue" data-testid="render-queue">
  <header>
    <h3>Render queue</h3>
    {#if exports.active.length > 1}
      <Button
        size="sm"
        variant="ghost"
        onclick={() => exports.cancelAll()}
        data-testid="cancel-all"
      >
        Cancel all
      </Button>
    {/if}
  </header>

  {#if rows.length === 0}
    <p class="empty" data-testid="queue-empty">
      Nothing is rendering. Files you export show up here with their progress.
    </p>
  {:else}
    <ul>
      {#each rows as row (row.id)}
        <li class={row.state} data-testid="queue-row" data-state={row.state}>
          <div class="line">
            <span class="name" title={row.name}>{row.name}</span>
            <span class="state" data-testid="queue-state">
              {#if row.state === 'rendering'}{percent(row.progress)}{:else}{LABEL[row.state]}{/if}
            </span>
            {#if isActive(row)}
              <Button
                size="sm"
                variant="ghost"
                onclick={() => exports.cancel(row.id)}
                aria-label={`Cancel ${row.name}`}
                data-testid="row-cancel"
              >
                Cancel
              </Button>
            {:else if row.retryable && (row.state === 'failed' || row.state === 'cancelled')}
              <Button
                size="sm"
                variant="ghost"
                onclick={() => exports.retry(row.id)}
                aria-label={`Retry ${row.name}`}
                data-testid="row-retry"
              >
                Retry
              </Button>
            {/if}
          </div>
          {#if row.state === 'rendering'}
            <ProgressBar value={row.progress} label={`Rendering ${row.name}`} />
          {:else if row.state === 'waiting'}
            <p class="detail">Waiting for a free slot.</p>
          {:else if row.state === 'done' && row.folder}
            <p class="detail">Saved in {row.folder}</p>
          {:else if row.state === 'failed'}
            <p class="problem" role="alert" data-testid="row-error">
              <Icon name="alert" size={14} />
              <span>
                {row.message}
                {#if row.fix}<strong>Fix:</strong> {row.fix}{/if}
              </span>
            </p>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .queue {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-height: 0;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  h3 {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-bold);
    color: var(--color-text-muted);
    text-transform: none;
  }
  .empty {
    margin: 0;
    color: var(--color-text-muted);
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  li {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    background: var(--color-surface);
  }
  li.failed {
    border-color: var(--color-danger);
  }
  .line {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-height: var(--control-height-sm);
  }
  .name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: var(--font-weight-medium);
  }
  .state {
    color: var(--color-text-muted);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .done .state {
    color: var(--color-ok);
  }
  .failed .state {
    color: var(--color-danger);
  }
  .detail {
    margin: 0;
    color: var(--color-text-muted);
    font-size: var(--font-size-sm);
    overflow-wrap: anywhere;
  }
  .problem {
    display: flex;
    gap: var(--space-2);
    margin: 0;
    color: var(--color-danger);
    font-size: var(--font-size-sm);
    overflow-wrap: anywhere;
  }
  .problem :global(svg) {
    flex: none;
    margin-top: 2px;
  }
  .problem strong {
    color: var(--color-text);
  }
</style>
