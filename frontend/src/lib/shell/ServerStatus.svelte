<script lang="ts">
  import Button from '../ui/Button.svelte';
  import { ApiError, whoamiApiV1WhoamiGet, type WhoAmIOut } from '../api/client';

  // The top bar's status line. GET /api/v1/whoami is the lightest real endpoint: it answers without a project
  // and also shows whether the server wants a token (docs/api.md).
  type State =
    | { kind: 'loading' }
    | { kind: 'ready'; who: WhoAmIOut }
    | { kind: 'error'; message: string; fix: string };

  let state = $state<State>({ kind: 'loading' });

  async function load() {
    state = { kind: 'loading' };
    try {
      const { data } = await whoamiApiV1WhoamiGet();
      state = { kind: 'ready', who: data };
    } catch (error) {
      state =
        error instanceof ApiError
          ? { kind: 'error', message: error.message, fix: error.fix }
          : { kind: 'error', message: 'Unexpected error.', fix: '' };
    }
  }

  $effect(() => {
    void load();
  });
</script>

<span class="status" data-testid="server-status" data-state={state.kind} role="status">
  {#if state.kind === 'loading'}
    Connecting to the server…
  {:else if state.kind === 'ready'}
    <span class="dot ok" aria-hidden="true"></span>
    Server connected as {state.who.name ?? state.who.id}{state.who.auth_required
      ? ' (token required)'
      : ''}
  {:else}
    <span class="dot bad" aria-hidden="true"></span>
    <span class="error" title={state.fix}>Server unreachable: {state.message}</span>
    <Button size="sm" onclick={load}>Retry</Button>
  {/if}
</span>

<style>
  .status {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    margin-left: auto;
    color: var(--color-text-muted);
  }

  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--color-text-muted);
  }

  .dot.ok {
    background: var(--color-ok);
  }

  .dot.bad {
    background: var(--color-danger);
  }

  .error {
    color: var(--color-text);
  }
</style>
