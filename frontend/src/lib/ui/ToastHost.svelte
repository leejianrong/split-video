<script lang="ts">
  import { toasts } from '../state/toasts.svelte';
  import Button from './Button.svelte';
  import Icon, { type IconName } from './icons/Icon.svelte';

  // Mount once, near the root. The container is a polite live region that exists before any toast, so a toast
  // added later is announced. Timers pause while the pointer or focus is inside a toast.
  const icons: Record<string, IconName> = {
    info: 'info',
    success: 'check',
    warning: 'alert',
    error: 'alert',
  };
</script>

<div
  class="host"
  role="region"
  aria-label="Notifications"
  aria-live="polite"
  aria-relevant="additions text"
>
  {#each toasts.items as toast (toast.id)}
    <!-- Pausing is a pointer and focus convenience; every control inside is a real button. -->
    <div
      class="toast {toast.kind}"
      role="group"
      aria-label={toast.kind === 'error' ? 'Error' : undefined}
      data-kind={toast.kind}
      onmouseenter={() => toasts.pause(toast.id)}
      onmouseleave={() => toasts.resume(toast.id)}
      onfocusin={() => toasts.pause(toast.id)}
      onfocusout={() => toasts.resume(toast.id)}
    >
      <span class="icon"><Icon name={icons[toast.kind] ?? 'info'} /></span>
      <p>{toast.message}</p>
      {#if toast.action}
        {@const action = toast.action}
        <Button
          size="sm"
          variant="secondary"
          onclick={() => {
            action.onclick();
            toasts.dismiss(toast.id);
          }}>{action.label}</Button
        >
      {/if}
      <Button
        iconOnly
        size="sm"
        variant="ghost"
        aria-label="Dismiss notification"
        onclick={() => toasts.dismiss(toast.id)}><Icon name="close" size={16} /></Button
      >
    </div>
  {/each}
</div>

<style>
  .host {
    position: fixed;
    right: var(--space-4);
    bottom: var(--space-4);
    z-index: var(--z-toast);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    width: min(380px, calc(100vw - var(--space-6)));
    pointer-events: none;
  }

  .toast {
    pointer-events: auto;
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-2) var(--space-2) var(--space-3);
    border: 1px solid var(--color-border-strong);
    border-left-width: 4px;
    border-radius: var(--radius-md);
    background: var(--color-surface-raised);
    color: var(--color-text);
    box-shadow: var(--shadow-popover);
    animation: slide-in var(--duration-base) ease-out;
  }

  p {
    flex: 1;
    margin: 0;
  }

  .info {
    border-left-color: var(--color-accent);
  }
  .info .icon {
    color: var(--color-accent);
  }
  .success {
    border-left-color: var(--color-ok);
  }
  .success .icon {
    color: var(--color-ok);
  }
  .warning {
    border-left-color: var(--color-warn);
  }
  .warning .icon {
    color: var(--color-warn);
  }
  .error {
    border-left-color: var(--color-danger);
  }
  .error .icon {
    color: var(--color-danger);
  }

  .icon {
    display: inline-flex;
  }

  @keyframes slide-in {
    from {
      opacity: 0;
      transform: translateY(8px);
    }
  }
</style>
