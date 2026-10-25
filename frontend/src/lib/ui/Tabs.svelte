<script lang="ts" module>
  export interface TabItem {
    id: string;
    label: string;
    disabled?: boolean;
  }
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';

  // tablist / tab / tabpanel with a roving tabindex: Tab enters the selected tab, the arrow keys move and select
  // (Left/Right, or Up/Down when vertical), Home and End jump. Only the selected panel's content is rendered.
  type Props = {
    tabs: TabItem[];
    selected: string;
    label: string;
    orientation?: 'horizontal' | 'vertical';
    panel: Snippet<[string]>;
  };

  let { tabs, selected = $bindable(), label, orientation = 'horizontal', panel }: Props = $props();

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

<div class="tabs {orientation}">
  <div class="list" role="tablist" aria-label={label} aria-orientation={orientation}>
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
        onkeydown={(e) => onkeydown(e, tab.id)}>{tab.label}</button
      >
    {/each}
  </div>
  {#each tabs as tab (tab.id)}
    <div
      role="tabpanel"
      id={panelId(tab.id)}
      aria-labelledby={tabId(tab.id)}
      hidden={selected !== tab.id}
      tabindex="0"
      class="panel"
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
  .panel[hidden] {
    display: none;
  }
</style>
