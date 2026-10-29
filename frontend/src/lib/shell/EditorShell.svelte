<script lang="ts">
  import { registerAction } from '../a11y/dispatcher';
  import { limitsOf, MIN_WINDOW, type TabId } from '../state/layout';
  import { connectLive } from '../state/live';
  import { exports } from '../state/exports.svelte';
  import { ui } from '../state/ui.svelte';
  import ExportDialog from '../export/ExportDialog.svelte';
  import Tabs from '../ui/Tabs.svelte';
  import InspectorRegion from './InspectorRegion.svelte';
  import LeftPanel from './LeftPanel.svelte';
  import PreviewRegion from './PreviewRegion.svelte';
  import ResizeHandle from './ResizeHandle.svelte';
  import { TABS } from './tabs';
  import TimelineRegion from './TimelineRegion.svelte';
  import TopBar from './TopBar.svelte';

  // The editor: one shell of six regions (docs/ux-spec.md#layout). Sizes come from `ui`, which clamps them to the
  // window; the grid reads them as custom properties. Regions hold no logic: they read the stores.
  const sizes = $derived(ui.sizes);
  const panelLimits = $derived(limitsOf('panel', sizes, ui.viewport));
  const inspectorLimits = $derived(limitsOf('inspector', sizes, ui.viewport));
  const timelineLimits = $derived(limitsOf('timeline', sizes, ui.viewport));

  // One event stream for the editor's life: job progress and project changes (agent and CLI edits) arrive on it.
  $effect(() => connectLive());

  // The render queue: everything the server still remembers, so a reload shows finished and failed renders too.
  $effect(() => exports.watch());

  $effect(() => registerAction('project.export', () => void (ui.exportOpen = true)));
</script>

<div
  class="shell"
  style:--panel="{sizes.panel}px"
  style:--inspector="{sizes.inspector}px"
  style:--timeline="{sizes.timeline}px"
>
  <TopBar />

  <div class="left" data-region="left">
    <Tabs
      variant="rail"
      orientation="vertical"
      label="Editor tabs"
      tabs={TABS.map((t) => ({ id: t.id, label: t.label, icon: t.icon }))}
      bind:selected={() => ui.activeTab, (id: string) => ui.setTab(id as TabId)}
      regions={{ list: 'tab-rail', panel: 'panel' }}
    >
      {#snippet panel(id)}
        <LeftPanel tab={id as TabId} />
      {/snippet}
    </Tabs>
    <ResizeHandle
      label="Resize the left panel"
      orientation="vertical"
      edge="right"
      direction={1}
      value={sizes.panel}
      min={panelLimits.min}
      max={panelLimits.max}
      onchange={(v) => ui.setSize('panel', v)}
      onreset={() => ui.resetSize('panel')}
    />
  </div>

  <PreviewRegion />

  <aside class="inspector" data-region="inspector" aria-label="Inspector">
    <InspectorRegion />
    <ResizeHandle
      label="Resize the inspector"
      orientation="vertical"
      edge="left"
      direction={-1}
      value={sizes.inspector}
      min={inspectorLimits.min}
      max={inspectorLimits.max}
      onchange={(v) => ui.setSize('inspector', v)}
      onreset={() => ui.resetSize('inspector')}
    />
  </aside>

  <div class="timeline-area">
    <TimelineRegion />
    <ResizeHandle
      label="Resize the timeline"
      orientation="horizontal"
      edge="top"
      direction={-1}
      value={sizes.timeline}
      min={timelineLimits.min}
      max={timelineLimits.max}
      onchange={(v) => ui.setSize('timeline', v)}
      onreset={() => ui.resetSize('timeline')}
    />
  </div>
</div>

<div class="too-small" role="alert" data-testid="too-small">
  <h1>This window is too small</h1>
  <p>
    The editor needs a window at least {MIN_WINDOW.width} px wide. Make the window larger or zoom the
    page out.
  </p>
</div>

<ExportDialog />

<style>
  .shell {
    display: grid;
    height: 100vh;
    min-height: 720px;
    grid-template-columns:
      calc(var(--size-tabrail-width) + var(--panel))
      minmax(var(--size-preview-min), 1fr)
      var(--inspector);
    grid-template-rows: var(--size-topbar-height) minmax(0, 1fr) var(--timeline);
    grid-template-areas:
      'top top top'
      'left preview inspector'
      'timeline timeline timeline';
    overflow: hidden;
  }

  .shell > :global([data-region='top-bar']) {
    grid-area: top;
  }
  .shell > :global([data-region='preview']) {
    grid-area: preview;
  }
  .left {
    grid-area: left;
    position: relative;
    min-width: 0;
    min-height: 0;
    /* A shadow, not a border, so the panel keeps its exact 308 px. */
    box-shadow: 1px 0 0 var(--color-border);
  }
  .inspector {
    grid-area: inspector;
    position: relative;
    min-width: 0;
    min-height: 0;
    overflow: auto;
    box-shadow: -1px 0 0 var(--color-border);
    background: var(--color-surface);
  }
  .timeline-area {
    grid-area: timeline;
    position: relative;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .timeline-area > :global(section) {
    flex: 1;
  }

  /* Below 1280 px the editor says so instead of drawing a broken layout (ux-spec decision 9). */
  .too-small {
    display: none;
  }
  @media (max-width: 1279px) {
    .shell {
      display: none;
    }
    .too-small {
      display: grid;
      place-content: center;
      gap: var(--space-3);
      min-height: 100vh;
      padding: var(--space-6);
      text-align: center;
    }
  }
</style>
