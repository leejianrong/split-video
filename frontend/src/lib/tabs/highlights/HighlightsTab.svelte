<script lang="ts">
  import { untrack } from 'svelte';
  import { announce } from '../../a11y/announce';
  import { baseName } from '../../format';
  import { assets } from '../../state/assets.svelte';
  import { highlights, PRESETS } from '../../state/highlights.svelte';
  import { project } from '../../state/project.svelte';
  import { quickActions } from '../../state/quickActions.svelte';
  import { selection } from '../../state/selection.svelte';
  import { thumbs } from '../../state/thumbs.svelte';
  import { toasts } from '../../state/toasts.svelte';
  import { frameUrl } from '../../timeline/thumbs';
  import Button from '../../ui/Button.svelte';
  import Icon from '../../ui/icons/Icon.svelte';
  import ProgressBar from '../../ui/ProgressBar.svelte';
  import { EMPTY_COPY } from '../../shell/tabs';
  import HighlightCard from './HighlightCard.svelte';

  // The Highlights tab (ux-spec #highlights): the suggestions of "Find highlights" as cards to review. It works on the
  // selected library asset, or on the open recording when nothing is selected, like the quick actions do. The
  // suggestions come from stored signals (cheap, repeatable); what the user decided is in the video's sidecar.
  // The library may not be loaded yet (a reload can open straight on this tab); the open recording is always in it.
  $effect(() => {
    if (assets.status === 'idle') void assets.load();
  });
  $effect(() => {
    if (assets.status === 'ready' && project.id) void assets.ensure(project.id);
  });

  const target = $derived(assets.byId(selection.assetId) ?? assets.byPath(project.id));
  const run = $derived(target ? quickActions.view(target.id, 'highlights') : null);
  const running = $derived(
    run !== null &&
      (run.state === 'running' || run.state === 'queued' || run.state === 'submitting'),
  );
  let keywordOpen = $state(false);
  let keywordText = $state('');

  // Read the suggestions when the tab opens and whenever its asset changes. (A finished "Find highlights" reloads
  // them from the store, whether or not this tab is open.)
  let loadedFor: string | null = null;
  $effect(() => {
    const asset = target;
    if (!asset || !asset.present || loadedFor === asset.id) return;
    loadedFor = asset.id;
    untrack(() => void highlights.load(asset));
  });

  // Posters: the first frame of each card's range, a few at a time through the shared loader.
  $effect(() => {
    const id = highlights.asset?.id;
    thumbs.want('highlights', id ? highlights.visible.map((s) => frameUrl(id, s.from, 160)) : []);
    return () => thumbs.drop('highlights');
  });

  // Leaving the tab clears the range it drew on the timeline.
  $effect(() => () => highlights.select(null));

  async function rerun() {
    if (target) await quickActions.run('highlights', target);
  }

  async function makeAll() {
    const keys = highlights.pending.map((s) => s.key);
    const made = await highlights.make(keys, 'short');
    if (!made && highlights.actionError) {
      toasts.error(`${highlights.actionError.message} ${highlights.actionError.fix}`.trim());
    }
  }

  async function applyKeyword(event: Event) {
    event.preventDefault();
    await highlights.setKeyword(keywordText);
    announce(highlights.keyword ? `Looking for “${highlights.keyword}”` : 'Keyword filter cleared');
    if (!highlights.keyword) keywordOpen = false;
  }

  function onListKeydown(event: KeyboardEvent) {
    const step = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0;
    const current = (event.target as HTMLElement).closest('[data-testid="highlight-card"]');
    if (!step || !current || (event.target as HTMLElement).tagName !== 'BUTTON') return;
    if (!(event.target as HTMLElement).classList.contains('pick')) return;
    const cards = Array.from(
      (event.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(
        '[data-testid="highlight-card"]',
      ),
    );
    const next = cards[cards.indexOf(current as HTMLElement) + step];
    if (!next) return;
    event.preventDefault();
    next.querySelector<HTMLElement>('button.pick')?.focus();
  }

  const fixOf = (error: { message: string; fix: string }) =>
    error.fix && !error.message.includes(error.fix.slice(0, 40)) ? error.fix : '';
</script>

<section class="tab" aria-label="Highlights" data-testid="highlights-tab">
  <header class="head">
    <h2>Suggested highlights</h2>
    <Button
      size="sm"
      data-testid="rerun"
      disabled={!target || !target.present || running}
      title="Listen to the recording again and suggest new moments"
      onclick={rerun}
    >
      Re-run
    </Button>
  </header>

  {#if target}
    <p class="muted caption">From <strong title={target.path}>{baseName(target.path)}</strong></p>
  {/if}

  <div class="chips" role="group" aria-label="Kind of recording">
    {#each PRESETS as preset (preset.id)}
      <button
        type="button"
        class="chip"
        aria-pressed={highlights.preset === preset.id}
        onclick={() => highlights.setPreset(highlights.preset === preset.id ? null : preset.id)}
      >
        {preset.label}
      </button>
    {/each}
    <button
      type="button"
      class="chip"
      aria-pressed={highlights.keyword !== ''}
      aria-expanded={keywordOpen}
      onclick={() => (keywordOpen = !keywordOpen)}
    >
      {highlights.keyword ? `Keyword: ${highlights.keyword}` : 'Keyword…'}
    </button>
  </div>
  {#if keywordOpen}
    <form class="keyword" onsubmit={applyKeyword}>
      <label>
        <span class="sr-only">Word to look for in speech and on-screen text</span>
        <input
          bind:value={keywordText}
          placeholder="A word to look for"
          data-testid="keyword-input"
        />
      </label>
      <Button size="sm" type="submit">Find</Button>
    </form>
  {/if}

  {#if running && run}
    <div class="notice" role="status" data-testid="highlights-running">
      <div class="line">
        <span class="name">Finding highlights</span>
        <span class="muted"
          >{run.fraction !== null ? `${Math.round(run.fraction * 100)}%` : 'Working…'}</span
        >
        <Button
          size="sm"
          variant="ghost"
          disabled={run.cancelling}
          aria-label="Cancel Find highlights"
          onclick={() => target && quickActions.cancel(target.id, 'highlights')}
        >
          {run.cancelling ? 'Cancelling…' : 'Cancel'}
        </Button>
      </div>
      <ProgressBar value={run.fraction} label="Finding highlights" />
      {#if run.message}<span class="muted msg">{run.message}</span>{/if}
    </div>
  {:else if run?.state === 'error' && run.error}
    <div
      class="notice failed"
      role="group"
      aria-label="Find highlights failed"
      data-testid="highlights-failed"
    >
      <p class="lead"><Icon name="alert" size={14} /> <span>{run.error.message}</span></p>
      {#if fixOf(run.error)}<p class="muted">Fix: {fixOf(run.error)}</p>{/if}
      <span class="row">
        <Button size="sm" onclick={rerun}>Retry</Button>
        <Button
          size="sm"
          variant="ghost"
          onclick={() => target && quickActions.dismiss(target.id, 'highlights')}>Dismiss</Button
        >
      </span>
    </div>
  {/if}

  {#if !target}
    <p class="empty" data-testid="panel-empty">{EMPTY_COPY.highlights}</p>
  {:else if !target.present}
    <div class="notice failed" data-testid="highlights-missing-file">
      <p class="lead"><Icon name="alert" size={14} /> <span>The file was not found.</span></p>
      <p class="muted">
        Fix: move it back into the recordings folder, or use Locate in the Media tab.
      </p>
    </div>
  {:else if highlights.status === 'loading' && highlights.suggestions.length === 0}
    {#if !running}<p class="muted" role="status" data-testid="highlights-loading">
        Looking for highlights…
      </p>{/if}
  {:else if highlights.status === 'missing'}
    {#if !running}
      <div class="empty" data-testid="panel-empty">
        <p class="lead">{EMPTY_COPY.highlights}</p>
        <Button variant="primary" data-testid="find-highlights" onclick={rerun}
          >Find highlights</Button
        >
      </div>
    {/if}
  {:else if highlights.status === 'error' && highlights.error}
    <div class="notice failed" role="alert" data-testid="highlights-error">
      <p class="lead"><Icon name="alert" size={14} /> <span>{highlights.error.message}</span></p>
      {#if highlights.error.fix}<p class="muted">Fix: {highlights.error.fix}</p>{/if}
      <Button size="sm" onclick={() => target && highlights.load(target)}>Try again</Button>
    </div>
  {:else if highlights.status === 'ready'}
    {#if highlights.visible.length === 0}
      <div class="empty" data-testid="highlights-none">
        {#if highlights.dismissedCount > 0 && !highlights.showDismissed}
          <p class="lead">You dismissed every suggestion.</p>
          <Button size="sm" onclick={() => (highlights.showDismissed = true)}
            >Show {highlights.dismissedCount} dismissed</Button
          >
        {:else}
          <p class="lead">No highlight stood out in this recording.</p>
          <p class="muted">
            Fix: try another kind of recording above, or run Find highlights again after
            transcribing it.
          </p>
          {#each highlights.notes.slice(0, 2) as note (note)}<p class="muted msg">{note}</p>{/each}
        {/if}
      </div>
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <ul
        class="cards"
        aria-label="Suggested highlights"
        data-testid="highlight-list"
        onkeydown={onListKeydown}
      >
        {#each highlights.visible as card (card.key)}
          <HighlightCard {card} selected={highlights.selectedKey === card.key} />
        {/each}
      </ul>
      {#if highlights.dismissedCount > 0}
        <Button
          size="sm"
          variant="ghost"
          data-testid="toggle-dismissed"
          aria-pressed={highlights.showDismissed}
          onclick={() => (highlights.showDismissed = !highlights.showDismissed)}
        >
          {highlights.showDismissed ? 'Hide' : 'Show'}
          {highlights.dismissedCount} dismissed
        </Button>
      {/if}
      <Button
        data-testid="make-all"
        disabled={highlights.pending.length === 0 || highlights.busy || project.status !== 'ready'}
        onclick={makeAll}
      >
        Make a short from every highlight ({highlights.pending.length})
      </Button>
    {/if}
  {/if}
</section>

<style>
  .tab {
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
    margin: 0 auto 0 0;
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-bold);
  }

  p {
    margin: 0;
  }

  .muted {
    color: var(--color-text-muted);
  }

  .caption {
    overflow: hidden;
    font-size: var(--font-size-xs);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .caption strong {
    color: var(--color-text);
    font-weight: var(--font-weight-medium);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .chip {
    height: 26px;
    padding: 0 var(--space-3);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-full);
    background: var(--color-surface-raised);
    color: var(--color-text);
    font: inherit;
    font-size: var(--font-size-xs);
    cursor: pointer;
  }

  .chip[aria-pressed='true'] {
    border-color: var(--color-accent);
    color: var(--color-accent);
  }

  .keyword {
    display: flex;
    gap: var(--space-2);
  }

  .keyword label {
    flex: 1;
    min-width: 0;
  }

  .keyword input {
    width: 100%;
    height: var(--control-height-sm);
    padding: 0 var(--space-2);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-md);
    background: var(--color-bg);
    color: var(--color-text);
  }

  .cards {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
    margin: var(--space-5) 0;
    color: var(--color-text-muted);
    text-align: center;
  }

  .lead {
    display: flex;
    gap: var(--space-1);
    font-weight: var(--font-weight-medium);
  }

  .empty .lead {
    color: var(--color-text);
  }

  .notice {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding: var(--space-3);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    font-size: var(--font-size-sm);
  }

  .notice.failed {
    border-left: 3px solid var(--color-danger);
    background: var(--color-surface-raised);
  }

  .notice.failed .lead :global(svg) {
    flex: none;
    margin-top: 2px;
    color: var(--color-danger);
  }

  .line,
  .row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .line .muted {
    margin-left: auto;
    font-size: var(--font-size-xs);
  }

  .name {
    font-weight: var(--font-weight-medium);
  }

  .msg {
    font-size: var(--font-size-xs);
  }
</style>
