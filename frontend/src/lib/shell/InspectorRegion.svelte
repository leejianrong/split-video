<script lang="ts">
  import { formatLength } from '../format';
  import { project } from '../state/project.svelte';
  import { ui } from '../state/ui.svelte';
  import Button from '../ui/Button.svelte';
  import Panel from '../ui/Panel.svelte';

  // The inspector with nothing selected: the project's details and an Export shortcut (ux-spec #inspector).
  // Frame rate and "preview ready" are not in the project state yet, so they are not listed.
</script>

<div class="inspector-body">
  <p class="selection" data-testid="nothing-selected">Nothing selected</p>
  <Panel title="Project" level={2}>
    <dl>
      <dt>Name</dt>
      <dd>{project.project?.filename ?? ''}</dd>
      <dt>Source file</dt>
      <dd class="path">{project.id ?? ''}</dd>
      <dt>Length</dt>
      <dd>{project.project ? formatLength(project.project.duration) : ''}</dd>
      <dt>Canvas</dt>
      <dd>16:9</dd>
    </dl>
    <Button variant="secondary" onclick={() => (ui.exportOpen = true)}>Export</Button>
  </Panel>
</div>

<style>
  .inspector-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding: var(--space-4);
  }

  .selection {
    margin: 0;
    color: var(--color-text-muted);
  }

  dl {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--space-2) var(--space-4);
    margin: 0 0 var(--space-4);
  }

  dt {
    color: var(--color-text-muted);
  }

  dd {
    margin: 0;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
