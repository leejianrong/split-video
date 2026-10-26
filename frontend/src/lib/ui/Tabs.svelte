<script lang="ts" module>
  import type { IconName } from './icons/Icon.svelte';

  export interface TabItem {
    id: string;
    label: string;
    disabled?: boolean;
    /** Shown above the label in the `rail` variant. */
    icon?: IconName;
  }
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './icons/Icon.svelte';

  // tablist / tab / tabpanel with a roving tabindex: Tab enters the selected tab, the arrow keys move and select
  // (Left/Right, or Up/Down when vertical), Home and End jump. Only the selected panel's content is rendered.
  type Props = {
    tabs: TabItem[];
    selected: string;
    label: string;
    orientation?: 'horizontal' | 'vertical';
    /**
     * `rail` is the editor's left column: a fixed-width vertical strip of icon-over-label tabs inside a
     * `<nav>`, with the panel filling the rest of the height. Needs `orientation="vertical"`.
     */
    variant?: 'default' | 'rail';
    /** Tighter tabs and no panel padding, for a narrow column such as the inspector. */
    compact?: boolean;
    /** `data-region` values for the tab list and the panel (the shell names its regions this way). */
    regions?: { list?: string; panel?: string };
    panel: Snippet<[string]>;
  };

  let {
    tabs,
    selected = $bindable(),
    label,
    orientation = 'horizontal',
    variant = 'default',
    compact = false,
    regions,
    panel,
  }: Props = $props();

  const uid = $props.id();
  const tabId = (id: string) => `tab-${uid}-${id}`;
  const panelId = (id: string) => `tabpanel-${uid}-${id}`;
  let buttons: Record<string, HTMLButtonElement | undefined> = {};

  function move(from: string, step: 1 | -1 | 'first' | 'last') {
    const enabled = tabs.filter((t) => !t.disabled);
    if (!enabled.length) return;
    const index = enabled.findIndex((t) => t.id === from);
    const next =
      step === 'first'
        ? enabled[0]
        : step === 'last'
          ? enabled[enabled.length - 1]
          : enabled[(index + step + enabled.length) % enabled.length];
    if (!next) return;
    selected = next.id;
    buttons[next.id]?.focus();
  }

  function onkeydown(event: KeyboardEvent, id: string) {
    const [prev, next] =
      orientation === 'vertical' ? ['ArrowUp', 'ArrowDown'] : ['ArrowLeft', 'ArrowRight'];
    const step =
      event.key === prev
        ? -1
        : event.key === next
          ? 1
          : event.key === 'Home'
            ? 'first'
            : event.key === 'End'
              ? 'last'
              : null;
    if (step === null) return;
    // Handled here, so the global shortcut dispatcher (Home/End, arrows) leaves the key alone.
    event.preventDefault();
    move(id, step);
  }
</script>

{#snippet list()}
  <div
    class="list"
    role="tablist"
    aria-label={label}
    aria-orientation={orientation}
    data-region={variant === 'default' ? regions?.list : undefined}
  >
    {#each tabs as tab (tab.id)}
      <button
        bind:this={buttons[tab.id]}
        type="button"
        role="tab"
        id={tabId(tab.id)}
        aria-selected={selected === tab.id}
        aria-controls={panelId(tab.id)}
        tabindex={selected === tab.id ? 0 : -1}
        disabled={tab.disabled}
        class:selected={selected === tab.id}
        onclick={() => (selected = tab.id)}
        onkeydown={(e) => onkeydown(e, tab.id)}
        >{#if variant === 'rail' && tab.icon}<Icon name={tab.icon} size={20} />{/if}<span
          >{tab.label}</span
        ></button
      >
    {/each}
  </div>
{/snippet}

<div class="tabs {orientation} {variant}" class:compact>
  {#if variant === 'rail'}
    <nav aria-label={label} data-region={regions?.list}>{@render list()}</nav>
  {:else}
    {@render list()}
  {/if}
  {#each tabs as tab (tab.id)}
    <div
      role="tabpanel"
      id={panelId(tab.id)}
      aria-labelledby={tabId(tab.id)}
      hidden={selected !== tab.id}
      tabindex="0"
      class="panel"
      data-region={regions?.panel}
    >
      {#if selected === tab.id}{@render panel(tab.id)}{/if}
    </div>
  {/each}
</div>

<style>
  .list {
    display: flex;
    gap: var(--space-1);
    border-bottom: 1px solid var(--color-border);
  }

  .vertical {
    display: flex;
    gap: var(--space-3);
  }
  .vertical .list {
    flex-direction: column;
    border-bottom: 0;
    border-right: 1px solid var(--color-border);
    padding-right: var(--space-2);
  }

  button {
    padding: var(--space-2) var(--space-3);
    border: 0;
    border-bottom: 2px solid transparent;
    background: transparent;
    color: var(--color-text-muted);
    cursor: pointer;
  }
  .vertical button {
    border-bottom: 0;
    border-left: 2px solid transparent;
    text-align: left;
  }

  button:hover:not(:disabled) {
    background: var(--color-surface-hover);
    color: var(--color-text);
  }
  /* The selected tab has a teal edge (ux-spec: tab rail). */
  button.selected {
    color: var(--color-text);
    border-color: var(--color-accent);
  }
  button:disabled {
    color: var(--color-text-disabled);
    cursor: not-allowed;
  }

  .panel {
    padding: var(--space-3);
  }

  .compact .list {
    gap: 0;
  }
  .compact button {
    padding: var(--space-2) var(--space-1);
    flex: 1 1 auto;
    font-size: var(--font-size-sm);
  }
  .compact .panel {
    padding: 0;
  }

  /* Rail variant: the editor's left column (ux-spec #layout). The rail is 72 px, the panel takes the rest. */
  .rail {
    display: flex;
    gap: 0;
    height: 100%;
  }
  .rail nav {
    flex: none;
    width: var(--size-tabrail-width);
    border-right: 1px solid var(--color-border);
    background: var(--color-surface);
    overflow-y: auto;
  }
  .rail .list {
    flex-direction: column;
    gap: var(--space-0);
    height: 100%;
    padding: var(--space-2) 0;
    border: 0;
  }
  .rail button {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--space-1);
    /* 58 px in the wireframe; shrinks to keep all seven tabs visible in a 720 px window. */
    flex: 0 1 58px;
    min-height: 44px;
    padding: 0;
    border-left: 2px solid transparent;
    font-size: var(--font-size-xs);
    text-align: center;
  }
  .rail button.selected {
    color: var(--color-accent);
    border-color: var(--color-accent);
    background: var(--color-surface-raised);
  }
  .rail .panel {
    flex: 1;
    min-width: 0;
    padding: 0;
    overflow: auto;
    background: var(--color-surface);
  }
  .panel[hidden] {
    display: none;
  }
</style>
