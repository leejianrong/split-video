<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';

  type Props = Omit<HTMLInputAttributes, 'value'> & {
    label: string;
    value: string;
    hint?: string;
    /** An error says what went wrong and one thing to try (ux-spec principle 6). */
    error?: string;
  };

  let { label, value = $bindable(), hint, error, id: idProp, ...rest }: Props = $props();

  const uid = $props.id();
  const id = $derived(idProp ?? `field-${uid}`);
  const describedBy = $derived(error ? `${id}-error` : hint ? `${id}-hint` : undefined);
</script>

<div class="field">
  <label for={id}>{label}</label>
  <input
    {id}
    bind:value
    aria-invalid={error ? true : undefined}
    aria-describedby={describedBy}
    {...rest}
  />
  {#if error}
    <p id="{id}-error" class="error"><span aria-hidden="true">!</span> {error}</p>
  {:else if hint}
    <p id="{id}-hint" class="hint">{hint}</p>
  {/if}
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

  input {
    height: var(--control-height-md);
    padding: 0 var(--space-2);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-md);
    background: var(--color-bg);
    color: var(--color-text);
  }
  input:hover:not(:disabled) {
    background: var(--color-surface);
  }
  input[aria-invalid='true'] {
    border-color: var(--color-danger);
  }
  input:disabled {
    border-color: var(--color-border);
    color: var(--color-text-disabled);
    cursor: not-allowed;
  }

  p {
    margin: 0;
    font-size: var(--font-size-xs);
  }
  .hint {
    color: var(--color-text-muted);
  }
  .error {
    color: var(--color-danger);
  }
</style>
