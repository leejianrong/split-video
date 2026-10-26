<script lang="ts">
  import type { HTMLSelectAttributes } from 'svelte/elements';

  type Props = Omit<HTMLSelectAttributes, 'value' | 'children'> & {
    label: string;
    value: string;
    options: { value: string; label: string; disabled?: boolean }[];
  };

  let { label, value = $bindable(), options, id: idProp, ...rest }: Props = $props();

  const uid = $props.id();
  const id = $derived(idProp ?? `select-${uid}`);
</script>

<div class="field">
  <label for={id}>{label}</label>
  <select {id} bind:value {...rest}>
    {#each options as option (option.value)}
      <option value={option.value} disabled={option.disabled}>{option.label}</option>
    {/each}
  </select>
</div>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  label {
    font-size: var(--font-size-sm);
  }

  select {
    height: var(--control-height-md);
    padding: 0 var(--space-2);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-md);
    background: var(--color-bg);
    color: var(--color-text);
    cursor: pointer;
  }
  select:hover:not(:disabled) {
    background: var(--color-surface);
  }
  select:disabled {
    border-color: var(--color-border);
    color: var(--color-text-disabled);
    cursor: not-allowed;
  }
</style>
