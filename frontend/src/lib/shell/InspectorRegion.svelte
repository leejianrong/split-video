<script lang="ts">
  import { formatLength } from '../format';
  import { clipName } from '../timeline/lanes';
  import { clipSelection } from '../state/clipSelection.svelte';
  import { project } from '../state/project.svelte';
  import { ui } from '../state/ui.svelte';
  import ClipInspector from '../inspector/ClipInspector.svelte';
  import Button from '../ui/Button.svelte';
  import Panel from '../ui/Panel.svelte';

  // The contextual inspector (ux-spec #inspector). The header line names the selection ("Selected: Song 2, 4 min 12 s").
  // With nothing selected it shows the project's details and an Export shortcut (frame rate and "preview ready" are not
  // in the project state yet, so they are not listed); with clips selected, their controls (`ClipInspector`).
  const entries = $derived.by(() => {
    const tracks = project.timeline?.tracks ?? [];
    return clipSelection.ids.flatMap((id) => {
      for (const track of tracks) {
        const clip = track.clips.find((c) => c.id === id);
        if (clip) return [{ clip, track }];
      }
      return [];
    });
  });
  const selected = $derived(entries.map((e) => e.clip));
  const headline = $derived(
    selected.length === 0
      ? 'Nothing selected'
      : selected.length === 1
        ? `Selected: ${clipName(selected[0]!)}, ${formatLength(selected[0]!.duration)}`
        : `Selected: ${selected.length} clips, ${formatLength(selected.reduce((sum, c) => sum + c.duration, 0))}`,
  );
</script>

<div class="inspector-body">
  <p class="selection" data-testid={selected.length ? 'selection-summary' : 'nothing-selected'}>
    {headline}
  </p>
  {#if entries.length}
    <ClipInspector {entries} />
  {:else}
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
  {/if}
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
