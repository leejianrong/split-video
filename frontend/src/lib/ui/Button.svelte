<script lang="ts" module>
  export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
  export type ButtonSize = 'sm' | 'md' | 'lg';
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLButtonAttributes } from 'svelte/elements';

  type Props = Omit<HTMLButtonAttributes, 'children'> & {
    variant?: ButtonVariant;
    size?: ButtonSize;
    /** A square button holding only an icon. It then needs `aria-label`; prefer `IconButton`, which also adds the tooltip. */
    iconOnly?: boolean;
    children: Snippet;
  };

  let {
    variant = 'secondary',
    size = 'md',
    iconOnly = false,
    type = 'button',
    children,
    ...rest
  }: Props = $props();

  $effect(() => {
    if (iconOnly && !rest['aria-label'] && !rest['aria-labelledby']) {
      console.error('Button: an icon-only button needs aria-label');
    }
  });
</script>

<button {type} class="btn {variant} {size}" class:icon-only={iconOnly} {...rest}>
  {@render children()}
</button>

<style>
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    border: 1px solid transparent;
    border-radius: var(--radius-md);
    font-weight: var(--font-weight-medium);
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
    transition:
      background var(--duration-fast),
      border-color var(--duration-fast);
  }

  .sm {
    height: var(--control-height-sm);
    padding: 0 var(--space-2);
    font-size: var(--font-size-sm);
  }
  .md {
    height: var(--control-height-md);
    padding: 0 var(--space-3);
  }
  .lg {
    height: var(--control-height-lg);
    padding: 0 var(--space-4);
    font-size: var(--font-size-md);
  }

  .icon-only {
    padding: 0;
  }
  .icon-only.sm {
    width: var(--control-height-sm);
  }
  .icon-only.md {
    width: var(--control-height-md);
  }
  .icon-only.lg {
    width: var(--control-height-lg);
  }

  .primary {
    background: var(--color-accent);
    color: var(--color-accent-text);
  }
  .primary:hover:not(:disabled) {
    background: var(--color-accent-hover);
  }
  .primary:active:not(:disabled) {
    background: var(--color-accent-active);
  }

  .secondary {
    background: var(--color-surface-raised);
    border-color: var(--color-border-strong);
    color: var(--color-text);
  }
  .secondary:hover:not(:disabled) {
    background: var(--color-surface-hover);
  }
  .secondary:active:not(:disabled) {
    background: var(--color-surface-active);
  }

  .ghost {
    background: transparent;
    color: var(--color-text);
  }
  .ghost:hover:not(:disabled) {
    background: var(--color-surface-hover);
  }
  .ghost:active:not(:disabled) {
    background: var(--color-surface-active);
  }

  .danger {
    background: var(--color-danger-solid);
    color: var(--color-danger-text);
  }
  .danger:hover:not(:disabled) {
    background: var(--color-danger-hover);
  }

  .btn[aria-pressed='true'] {
    background: var(--color-accent-subtle);
    border-color: var(--color-accent);
    color: var(--color-text);
  }

  .btn:disabled {
    background: var(--color-surface);
    border-color: var(--color-border);
    color: var(--color-text-disabled);
    cursor: not-allowed;
  }
</style>
