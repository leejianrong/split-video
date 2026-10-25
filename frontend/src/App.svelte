<script lang="ts">
  import { registerAction } from './lib/a11y/dispatcher';
  import ServerStatus from './lib/shell/ServerStatus.svelte';
  import { ui } from './lib/state/ui.svelte';
  import ShortcutHelp from './lib/ui/ShortcutHelp.svelte';
  import ToastHost from './lib/ui/ToastHost.svelte';

  // `?` opens and closes the shortcut list (docs/ux-spec.md#shortcuts); the first action wired through the registry.
  $effect(() => registerAction('help.toggle', () => void (ui.helpOpen = !ui.helpOpen)));
</script>

<!--
  SCAFFOLD (SPL-59). Placeholder shell: the six regions from docs/ux-spec.md#layout as empty frames, no features.
  Later cards replace each frame with the real component under src/lib/.
-->
<div class="shell">
  <header class="topbar" data-region="top-bar">
    <strong>split-video</strong>
    <span class="scaffold">Scaffold: the new editor shell (SPL-59). No features yet.</span>
    <span class="hint">Press <kbd>?</kbd> for shortcuts</span>
    <ServerStatus />
  </header>
  <nav class="tabrail" data-region="tab-rail" aria-label="Tabs">Tab rail</nav>
  <aside class="panel" data-region="panel" aria-label="Panel">Panel</aside>
  <main class="preview" data-region="preview">Preview</main>
  <aside class="inspector" data-region="inspector" aria-label="Inspector">Inspector</aside>
  <section class="timeline" data-region="timeline" aria-label="Timeline">Timeline</section>
</div>

<!-- Overlays sit outside the grid so the region boxes stay as the wireframe has them. -->
<ShortcutHelp />
<ToastHost />

<style>
  .shell {
    display: grid;
    height: 100vh;
    grid-template-columns:
      var(--size-tabrail-width) var(--size-panel-width) minmax(480px, 1fr)
      var(--size-inspector-width);
    grid-template-rows: var(--size-topbar-height) 1fr var(--size-timeline-height);
    grid-template-areas:
      'top top top top'
      'rail panel preview inspector'
      'timeline timeline timeline timeline';
  }

  .shell > * {
    border: 1px solid var(--color-border);
    padding: var(--space-3);
    background: var(--color-surface);
    color: var(--color-text-muted);
    min-width: 0;
  }

  .topbar {
    grid-area: top;
    display: flex;
    align-items: center;
    gap: var(--space-4);
    color: var(--color-text);
  }

  .scaffold {
    color: var(--color-text-muted);
  }

  .hint {
    font-size: var(--font-size-sm);
  }

  .tabrail {
    grid-area: rail;
  }

  .panel {
    grid-area: panel;
  }

  .preview {
    grid-area: preview;
    background: var(--color-bg);
  }

  .inspector {
    grid-area: inspector;
  }

  .timeline {
    grid-area: timeline;
    background: var(--color-surface-raised);
  }
</style>
