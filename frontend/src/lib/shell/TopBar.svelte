<script lang="ts">
  import { formatLength } from '../format';
  import { hrefTo } from '../state/route';
  import { exports } from '../state/exports.svelte';
  import { project } from '../state/project.svelte';
  import { ui } from '../state/ui.svelte';
  import Button from '../ui/Button.svelte';
  import Icon from '../ui/icons/Icon.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Tooltip from '../ui/Tooltip.svelte';
  import ServerStatus from './ServerStatus.svelte';

  // The top bar (docs/ux-spec.md#layout): logo, project name and length, save state, canvas ratio, server status,
  // help and Export. Everything it shows comes from the project store; nothing here edits.
  const saved = $derived(project.project?.resumed ? 'Saved' : 'No changes yet');
</script>

<header class="bar" data-region="top-bar">
  <a class="logo" href={hrefTo({ view: 'start' })}>split-video</a>
  <a class="library" href={hrefTo({ view: 'library', dir: '' })}>
    <Icon name="folder" size={16} /> Library
  </a>

  {#if project.project}
    <span class="name" data-testid="project-name">{project.project.filename}</span>
    <span class="muted" data-testid="project-length">{formatLength(project.project.duration)}</span>
    <span
      class="muted saved"
      data-testid="saved-state"
      title={`Project revision ${project.revision}`}
    >
      <Icon name="check" size={14} />
      {saved}
    </span>
  {/if}

  <div class="right">
    <Tooltip label="Canvas ratio: 16:9 (changing it comes later)">
      <Button size="sm" disabled aria-label="Canvas ratio: 16:9, fixed for now" data-testid="ratio">
        16:9 <Icon name="chevronDown" size={14} />
      </Button>
    </Tooltip>
    <ServerStatus />
    <IconButton
      label="Keyboard shortcuts"
      icon="help"
      action="help.toggle"
      onclick={() => (ui.helpOpen = true)}
    />
    <Button
      variant="primary"
      onclick={() => (ui.exportOpen = true)}
      aria-label={exports.active.length
        ? `Export and render queue, ${exports.active.length} rendering`
        : 'Export'}
      data-testid="export-button"
    >
      Export
      {#if exports.active.length}
        <span class="badge" data-testid="queue-badge">{exports.active.length}</span>
      {/if}
    </Button>
  </div>
</header>

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    min-width: 0;
    padding: 0 var(--space-4);
    border-bottom: 1px solid var(--color-border);
    background: var(--color-surface);
  }

  .logo {
    color: var(--color-text);
    font-weight: var(--font-weight-bold);
    font-size: var(--font-size-md);
    text-decoration: none;
  }

  .library {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    color: var(--color-text-muted);
    text-decoration: none;
  }
  .library:hover {
    color: var(--color-text);
  }

  .name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: var(--font-weight-medium);
  }

  .muted {
    color: var(--color-text-muted);
    white-space: nowrap;
  }

  .saved {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
  }

  .badge {
    min-width: 18px;
    margin-left: var(--space-1);
    padding: 0 5px;
    border-radius: var(--radius-full);
    background: var(--color-accent-text);
    color: var(--color-accent);
    font-size: var(--font-size-sm);
    font-variant-numeric: tabular-nums;
    text-align: center;
  }

  .right {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    margin-left: auto;
    flex: none;
  }
</style>
