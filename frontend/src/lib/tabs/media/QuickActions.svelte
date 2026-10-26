<script lang="ts">
  import { baseName } from '../../format';
  import { assets } from '../../state/assets.svelte';
  import { project } from '../../state/project.svelte';
  import { ACTIONS, quickActions, type ActionDef } from '../../state/quickActions.svelte';
  import { selection } from '../../state/selection.svelte';
  import Button from '../../ui/Button.svelte';
  import Icon from '../../ui/icons/Icon.svelte';
  import ProgressBar from '../../ui/ProgressBar.svelte';

  // The four quick actions (ux-spec #left-tabs): each acts on the selected asset, or on the open recording when
  // nothing is selected, runs as a job on the server and shows its progress, a Cancel that really stops it, and an
  // error with the catalogue's fix and a Retry right here.
  const target = $derived(assets.byId(selection.assetId) ?? assets.byPath(project.id));
  const targetName = $derived(target ? baseName(target.path) : '');

  /** Why an action cannot run on the target, or '' when it can. */
  function blocked(action: ActionDef): string {
    if (!target) return 'Add a recording first';
    if (!target.present) return 'The file was not found';
    if ((action.id === 'split' || action.id === 'songs') && !target.has_video) {
      return 'Needs a video, not an audio file';
    }
    if (!target.has_audio) return 'This file has no sound';
    return '';
  }

  // The catalogue's message sometimes already carries the fix (the install hint); do not say it twice.
  const fixOf = (error: { message: string; fix: string }) =>
    error.fix && !error.message.includes(error.fix.slice(0, 40)) ? error.fix : '';

  const shown = $derived(
    target
      ? ACTIONS.map((a) => ({ def: a, view: quickActions.view(target.id, a.id) })).filter(
          (r) => r.view.state !== 'idle',
        )
      : [],
  );

  const STATE_WORDS: Record<string, string> = {
    submitting: 'Starting…',
    queued: 'Waiting for a free worker…',
    running: 'Working…',
  };
</script>

<section class="auto" aria-label="Quick actions" data-testid="quick-actions">
  <span class="muted caption">
    Quick actions on {#if target}<strong title={target.path}>{targetName}</strong>{:else}the
      selected clip{/if}
  </span>
  <div class="buttons">
    {#each ACTIONS as action (action.id)}
      {@const reason = blocked(action)}
      {@const busy = target ? quickActions.isBusy(target.id, action.id) : false}
      <Button
        size="md"
        disabled={reason !== '' || busy}
        data-action={action.id}
        title={reason ? `${action.label}: ${reason}` : action.hint}
        onclick={() => target && quickActions.run(action.id, target)}
      >
        {action.label}
      </Button>
    {/each}
  </div>

  {#if target}
    <ul class="runs" aria-label="Quick action progress">
      {#each shown as { def, view } (def.id)}
        <li class="run" data-testid="run" data-action={def.id} data-state={view.state}>
          <div class="line">
            <span class="name">{def.label}</span>
            {#if view.state === 'running' || view.state === 'queued' || view.state === 'submitting'}
              <span class="muted state">
                {#if view.fraction !== null}{Math.round(view.fraction * 100)}%{:else}{STATE_WORDS[
                    view.state
                  ]}{/if}
              </span>
              {#if view.state !== 'submitting'}
                <Button
                  size="sm"
                  variant="ghost"
                  data-testid="cancel-run"
                  disabled={view.cancelling}
                  aria-label={`Cancel ${def.label}`}
                  onclick={() => quickActions.cancel(target.id, def.id)}
                >
                  {view.cancelling ? 'Cancelling…' : 'Cancel'}
                </Button>
              {/if}
            {:else if view.state === 'done'}
              <span class="ok"><Icon name="check" size={14} /> {view.summary ?? 'Done'}</span>
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Dismiss ${def.label}`}
                onclick={() => quickActions.dismiss(target.id, def.id)}
              >
                OK
              </Button>
            {:else if view.state === 'cancelled'}
              <span class="muted state">Cancelled</span>
              <Button size="sm" variant="ghost" onclick={() => quickActions.run(def.id, target)}
                >Run again</Button
              >
            {/if}
          </div>
          {#if view.state === 'running' || view.state === 'queued' || view.state === 'submitting'}
            <ProgressBar value={view.fraction} label={`${def.label} progress`} />
            {#if view.message}<span class="muted msg">{view.message}</span>{/if}
          {:else if view.state === 'error' && view.error}
            <div class="error" role="group" aria-label={`${def.label} failed`}>
              <p class="head"><Icon name="alert" size={14} /> <span>{view.error.message}</span></p>
              {#if fixOf(view.error)}<p class="fix" data-testid="run-fix">
                  Fix: {fixOf(view.error)}
                </p>{/if}
              <span class="actions">
                <Button size="sm" onclick={() => quickActions.run(def.id, target)}>Retry</Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onclick={() => quickActions.dismiss(target.id, def.id)}
                >
                  Dismiss
                </Button>
              </span>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .auto {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-3);
    border: 1px dashed var(--color-border-strong);
    border-radius: var(--radius-lg);
  }

  .muted {
    color: var(--color-text-muted);
  }

  .caption {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .caption strong {
    color: var(--color-text);
    font-weight: var(--font-weight-medium);
  }

  .buttons {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--space-2);
  }

  .buttons :global(.btn) {
    min-width: 0;
    padding: 0 var(--space-2);
  }

  .runs {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .run {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding-top: var(--space-2);
    border-top: 1px solid var(--color-border);
    font-size: var(--font-size-sm);
  }

  .line {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .name {
    font-weight: var(--font-weight-medium);
  }

  .state {
    margin-left: auto;
    font-size: var(--font-size-xs);
  }

  .line :global(.btn) {
    flex: none;
  }

  .ok {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    margin-left: auto;
    color: var(--color-ok);
  }

  .msg {
    font-size: var(--font-size-xs);
  }

  .error {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding: var(--space-2);
    border-left: 3px solid var(--color-danger);
    border-radius: var(--radius-sm);
    background: var(--color-surface-raised);
  }

  .error p {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .head {
    display: flex;
    gap: var(--space-1);
  }

  .head :global(svg) {
    flex: none;
    margin-top: 2px;
    color: var(--color-danger);
  }

  .fix {
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
  }

  .actions {
    display: flex;
    gap: var(--space-1);
  }
</style>
