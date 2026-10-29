<script lang="ts">
  import type { Snippet } from 'svelte';
  import { pushScope } from '../a11y/dispatcher';
  import IconButton from './IconButton.svelte';

  // A modal dialog on the native <dialog>: showModal() makes the page behind inert and traps focus, Escape
  // closes it, and focus goes back to what opened it. While open it also takes the editor shortcuts out of play
  // (the dispatcher scope), so S or Delete cannot edit the timeline behind it.
  type Props = {
    open: boolean;
    title: string;
    description?: string;
    onclose?: () => void;
    /** Close on a click outside the box. Off for dialogs that hold unsaved input. */
    dismissOnBackdrop?: boolean;
    /** The widest the box gets, in px (default 560). */
    width?: number;
    children: Snippet;
    footer?: Snippet;
  };

  let {
    open = $bindable(false),
    title,
    description,
    onclose,
    dismissOnBackdrop = true,
    width = 560,
    children,
    footer,
  }: Props = $props();

  const uid = $props.id();
  let dialog: HTMLDialogElement | undefined = $state();
  let returnTo: Element | null = null;
  let popScope: (() => void) | null = null;

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      returnTo = document.activeElement;
      popScope = pushScope('dialog');
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  });

  // Leaving the page state tidy if the component is destroyed while open.
  $effect(() => () => popScope?.());

  function handleClose() {
    popScope?.();
    popScope = null;
    open = false;
    if (returnTo instanceof HTMLElement && returnTo.isConnected) returnTo.focus();
    returnTo = null;
    onclose?.();
  }

  function onBackdrop(event: MouseEvent) {
    // A click on the <dialog> element itself, not on its content, is a click on the backdrop.
    if (dismissOnBackdrop && event.target === dialog) dialog?.close();
  }
</script>

<!-- Keyboard users close with Escape (native), so the backdrop click is a pointer convenience only. -->
<dialog
  bind:this={dialog}
  aria-labelledby="dlg-title-{uid}"
  aria-describedby={description ? `dlg-desc-${uid}` : undefined}
  onclose={handleClose}
  onclick={onBackdrop}
  style:--dialog-width="{width}px"
>
  <div class="box">
    <header>
      <h2 id="dlg-title-{uid}">{title}</h2>
      <IconButton label="Close" icon="close" size="sm" onclick={() => dialog?.close()} />
    </header>
    {#if description}<p id="dlg-desc-{uid}" class="description">{description}</p>{/if}
    <div class="content">{@render children()}</div>
    {#if footer}<footer>{@render footer()}</footer>{/if}
  </div>
</dialog>

<style>
  dialog {
    width: min(var(--dialog-width, 560px), calc(100vw - var(--space-6)));
    max-height: calc(100vh - var(--space-6));
    padding: 0;
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-lg);
    background: var(--color-surface-raised);
    color: var(--color-text);
    box-shadow: var(--shadow-dialog);
    overflow: hidden;
  }

  dialog[open] {
    display: flex;
  }

  dialog::backdrop {
    background: var(--color-overlay);
  }

  .box {
    display: flex;
    flex-direction: column;
    width: 100%;
    max-height: calc(100vh - var(--space-6));
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-3) var(--space-3) var(--space-4);
    border-bottom: 1px solid var(--color-border);
  }

  h2 {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-bold);
  }

  .description {
    margin: 0;
    padding: var(--space-3) var(--space-4) 0;
    color: var(--color-text-muted);
  }

  .content {
    padding: var(--space-4);
    overflow: auto;
  }

  footer {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    border-top: 1px solid var(--color-border);
  }
</style>
