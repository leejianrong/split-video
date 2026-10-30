<script lang="ts">
  import { tick } from 'svelte';
  import { announce } from '../../a11y/announce';
  import { formatClock, formatLength, parseClock } from '../../format';
  import { highlights, type Suggestion } from '../../state/highlights.svelte';
  import { playhead } from '../../state/playhead.svelte';
  import { project } from '../../state/project.svelte';
  import { thumbs } from '../../state/thumbs.svelte';
  import { toasts } from '../../state/toasts.svelte';
  import { frameUrl } from '../../timeline/thumbs';
  import Button from '../../ui/Button.svelte';
  import Icon from '../../ui/icons/Icon.svelte';

  // One suggestion (ux-spec #highlights): score, title, time range, why it was chosen and what to do with it.
  // Picking the card moves the playhead to it; hovering shows its range on the timeline. Make short and Add to
  // timeline are engine calls that also accept it; Accept only marks it; Dismiss removes it (Undo in the toast).
  let { card, selected }: { card: Suggestion; selected: boolean } = $props();

  const assetId = $derived(highlights.asset?.id ?? '');
  const poster = $derived(assetId ? frameUrl(assetId, card.from, 160) : '');
  const loaded = $derived(poster !== '' && thumbs.isLoaded(poster));
  const range = $derived(`${formatClock(card.from)} – ${formatClock(card.to)}`);

  const reason = $derived(card.reasons.slice(0, 2).join(' · '));
  const dismissed = $derived(card.state === 'dismissed');

  let adjusting = $state(false);
  let startText = $state('');
  let endText = $state('');
  let adjustError = $state('');
  let startInput: HTMLInputElement | undefined = $state();

  async function openAdjust() {
    startText = formatClock(card.from);
    endText = formatClock(card.to);
    adjustError = '';
    adjusting = true;
    await tick();
    startInput?.focus();
  }

  async function saveAdjust() {
    const from = parseClock(startText);
    const to = parseClock(endText);
    const limit = highlights.asset?.duration ?? Infinity;
    if (from === null || to === null) {
      adjustError = 'Type times like 4:10 or 1:02:03. Fix: check the start and the end.';
      return;
    }
    if (to - from < 1) {
      adjustError = 'A highlight needs at least one second. Fix: move the end after the start.';
      return;
    }
    if (to > limit + 0.001) {
      adjustError = `The recording ends at ${formatClock(limit)}. Fix: use an earlier end.`;
      return;
    }
    if (await highlights.adjust(card.key, from, to)) {
      adjusting = false;
      announce(`Highlight now ${formatClock(from)} to ${formatClock(to)}`);
    } else adjustError = actionMessage();
  }

  async function resetAdjust() {
    if (await highlights.adjust(card.key, card.start, card.end)) {
      adjusting = false;
      announce('Highlight back to the suggested range');
    } else adjustError = actionMessage();
  }

  const actionMessage = () =>
    `${highlights.actionError?.message ?? ''} ${highlights.actionError?.fix ?? ''}`.trim();

  function useHere(which: 'start' | 'end') {
    const text = formatClock(playhead.time);
    if (which === 'start') startText = text;
    else endText = text;
  }

  async function make(kind: 'short' | 'clip') {
    const made = await highlights.make([card.key], kind);
    if (!made && highlights.actionError) {
      toasts.error(`${highlights.actionError.message} ${highlights.actionError.fix}`.trim());
    }
  }

  async function accept() {
    if (await highlights.review(card.key, 'accepted')) announce(`Accepted ${card.title}`);
  }

  async function dismiss() {
    if (!(await highlights.review(card.key, 'dismissed'))) return;
    announce(`Dismissed ${card.title}`);
    toasts.show({
      message: `Dismissed “${card.title}”`,
      action: { label: 'Undo', onclick: () => void highlights.review(card.key, 'pending') },
    });
  }

  // The button's name starts with its visible text in the visible order (WCAG 2.5.3, Label in Name), then the state.
  const pickName = $derived(
    [
      `${card.score} ${card.title} ${range} · ${formatLength(card.to - card.from)}`,
      card.adjusted ? 'adjusted' : '',
      reason,
    ]
      .filter(Boolean)
      .join(' ') + `, ${card.state === 'pending' ? 'not reviewed' : card.state}`,
  );

  const blockedReason = $derived(
    project.status !== 'ready' ? 'Open a recording first' : highlights.busy ? 'Working…' : '',
  );
</script>

<li
  class="card"
  class:selected
  class:dismissed
  data-testid="highlight-card"
  data-key={card.key}
  data-state={card.state}
  onpointerenter={() => highlights.hover(card.key)}
  onpointerleave={() => highlights.hover(null)}
>
  <button
    type="button"
    class="pick"
    aria-pressed={selected}
    aria-label={pickName}
    onclick={() =>
      highlights.select(
        card.key,
        project.timeline?.tracks.flatMap((t) => t.clips),
      )}
  >
    <span class="thumb">
      {#if loaded}
        <img src={poster} alt="" draggable="false" data-testid="highlight-poster" />
      {:else}
        <Icon name="video" size={22} />
      {/if}
      <span class="score" title="Score out of 100">{card.score}</span>
    </span>
    <span class="text">
      <span class="title">{card.title}</span>
      <span class="range" data-testid="highlight-range">
        {range} · {formatLength(card.to - card.from)}{#if card.adjusted}
          <span class="tag">adjusted</span>{/if}
      </span>
      {#if reason}<span class="why">{reason}</span>{/if}
    </span>
  </button>

  <div class="actions">
    {#if dismissed}
      <span class="state"><Icon name="close" size={14} /> Dismissed</span>
      <Button size="sm" onclick={() => highlights.review(card.key, 'pending')}>Restore</Button>
    {:else}
      <Button
        size="sm"
        variant="primary"
        data-testid="make-short"
        disabled={blockedReason !== ''}
        title={blockedReason || 'Add this range to the timeline as a vertical 9:16 short'}
        onclick={() => make('short')}
      >
        Make short
      </Button>
      <Button
        size="sm"
        data-testid="add-clip"
        disabled={blockedReason !== ''}
        title={blockedReason || 'Add this range to the timeline as a labelled segment'}
        onclick={() => make('clip')}
      >
        Add to timeline
      </Button>
      <span class="small">
        {#if card.state === 'accepted'}
          <span class="state ok" data-testid="accepted"
            ><Icon name="check" size={14} /> Accepted</span
          >
        {:else}
          <Button size="sm" variant="ghost" data-testid="accept" onclick={accept}>Accept</Button>
        {/if}
        <Button
          size="sm"
          variant="ghost"
          data-testid="adjust"
          aria-expanded={adjusting}
          onclick={() => (adjusting ? (adjusting = false) : openAdjust())}
        >
          Adjust
        </Button>
        <Button size="sm" variant="ghost" data-testid="dismiss" onclick={dismiss}>Dismiss</Button>
      </span>
    {/if}
  </div>

  {#if adjusting}
    <form
      class="adjust"
      aria-label={`Adjust ${card.title}`}
      onsubmit={(event) => {
        event.preventDefault();
        void saveAdjust();
      }}
    >
      <div class="times">
        <label>
          Start
          <input bind:this={startInput} bind:value={startText} inputmode="decimal" />
        </label>
        <label>
          End
          <input bind:value={endText} inputmode="decimal" />
        </label>
      </div>
      <div class="here">
        <Button size="sm" variant="ghost" onclick={() => useHere('start')}>Start at playhead</Button
        >
        <Button size="sm" variant="ghost" onclick={() => useHere('end')}>End at playhead</Button>
      </div>
      {#if adjustError}<p class="error" role="alert" data-testid="adjust-error">
          <Icon name="alert" size={14} />
          {adjustError}
        </p>{/if}
      <div class="here">
        <Button size="sm" variant="primary" type="submit">Save</Button>
        {#if card.adjusted}<Button size="sm" onclick={resetAdjust}>Use suggested range</Button>{/if}
        <Button size="sm" variant="ghost" onclick={() => (adjusting = false)}>Cancel</Button>
      </div>
    </form>
  {/if}
</li>

<style>
  .card {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-2);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    background: var(--color-surface-raised);
    list-style: none;
  }

  .card.selected {
    border-color: var(--color-accent);
  }

  .card.dismissed {
    opacity: 0.8;
  }

  .pick {
    display: flex;
    gap: var(--space-2);
    width: 100%;
    padding: 0;
    border: 0;
    border-radius: var(--radius-md);
    background: none;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .thumb {
    position: relative;
    display: grid;
    flex: none;
    place-items: center;
    width: 72px;
    height: 96px;
    overflow: hidden;
    border-radius: var(--radius-md);
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

  .score {
    position: absolute;
    left: var(--space-1);
    top: var(--space-1);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
    background: var(--color-accent);
    color: var(--color-accent-text);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-bold);
  }

  .text {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }

  .title {
    overflow: hidden;
    font-weight: var(--font-weight-bold);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .range {
    font-variant-numeric: tabular-nums;
    font-size: var(--font-size-xs);
  }

  .why {
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
    overflow-wrap: anywhere;
  }

  .tag {
    margin-left: var(--space-1);
    padding: 0 var(--space-1);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-sm);
    color: var(--color-text-muted);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1);
  }

  .small {
    display: flex;
    flex-basis: 100%;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1);
  }

  .state {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 0 var(--space-2);
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
  }

  .state.ok {
    color: var(--color-ok);
  }

  .adjust {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding-top: var(--space-2);
    border-top: 1px solid var(--color-border);
  }

  .times {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-2);
  }

  label {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    font-size: var(--font-size-xs);
  }

  input {
    height: var(--control-height-sm);
    min-width: 0;
    padding: 0 var(--space-2);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-md);
    background: var(--color-bg);
    color: var(--color-text);
    font-variant-numeric: tabular-nums;
  }

  .here {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .error {
    display: flex;
    gap: var(--space-1);
    margin: 0;
    color: var(--color-danger);
    font-size: var(--font-size-xs);
  }

  .error :global(svg) {
    flex: none;
    margin-top: 2px;
  }
</style>
