<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';

  type Props = Omit<HTMLInputAttributes, 'type' | 'checked' | 'children'> & {
    label: string;
    checked: boolean;
  };

  let { label, checked = $bindable(), disabled = false, ...rest }: Props = $props();
</script>

<label class:disabled>
  <input type="checkbox" bind:checked {disabled} {...rest} />
  <span>{label}</span>
</label>

<style>
  label {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    min-height: var(--control-height-sm);
    cursor: pointer;
  }
  label.disabled {
    color: var(--color-text-disabled);
    cursor: not-allowed;
  }

  input {
    width: 16px;
    height: 16px;
    margin: 0;
    accent-color: var(--color-accent);
    cursor: inherit;
  }
</style>
