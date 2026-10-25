<script lang="ts">
  import { detectMac } from '../a11y/dispatcher';
  import { displayKeys, SHORTCUT_GROUPS, SHORTCUTS } from '../a11y/shortcuts';
  import { ui } from '../state/ui.svelte';
  import Dialog from './Dialog.svelte';

  // The `?` overlay. Generated from the registry, so it lists exactly the table in docs/ux-spec.md#shortcuts.
  const mac = detectMac();
  const groups = SHORTCUT_GROUPS.map((group) => ({
    group,
    entries: SHORTCUTS.filter((s) => s.group === group),
  })).filter((g) => g.entries.length > 0);
</script>

<Dialog
  bind:open={ui.helpOpen}
  title="Keyboard shortcuts"
  description="Shortcuts are off while you type in a text field, except Escape and Ctrl or Cmd combinations. On macOS Ctrl reads as Cmd."
>
  <div class="groups">
    {#each groups as { group, entries } (group)}
      <section aria-labelledby="sc-{group.replaceAll(' ', '-')}">
        <h3 id="sc-{group.replaceAll(' ', '-')}">{group}</h3>
        <dl>
          {#each entries as entry (entry.id)}
            <div class="row">
              <dt>{entry.label}</dt>
              <dd>
                {#each displayKeys(entry, mac) as key, i (i)}
                  {#if i > 0}<span class="or"> / </span>{/if}<kbd>{key}</kbd>
                {/each}
              </dd>
            </div>
          {/each}
        </dl>
      </section>
    {/each}
  </div>
</Dialog>

<style>
  .groups {
    display: grid;
    gap: var(--space-4);
  }

  h3 {
    margin-bottom: var(--space-2);
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-bold);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  dl {
    margin: 0;
  }

  .row {
    display: flex;
    justify-content: space-between;
    gap: var(--space-4);
    padding: var(--space-1) 0;
    border-bottom: 1px solid var(--color-border);
  }

  dt {
    color: var(--color-text);
  }

  dd {
    margin: 0;
    text-align: right;
    white-space: nowrap;
  }

  .or {
    color: var(--color-text-muted);
  }
</style>
