<script lang="ts">
  import { registerAction } from './lib/a11y/dispatcher';
  import StartView from './lib/firstrun/StartView.svelte';
  import LibraryView from './lib/library/LibraryView.svelte';
  import EditorShell from './lib/shell/EditorShell.svelte';
  import ProjectState from './lib/shell/ProjectState.svelte';
  import { project } from './lib/state/project.svelte';
  import { router } from './lib/state/route.svelte';
  import { ui } from './lib/state/ui.svelte';
  import ShortcutHelp from './lib/ui/ShortcutHelp.svelte';
  import ToastHost from './lib/ui/ToastHost.svelte';

  // `?` opens and closes the shortcut list (docs/ux-spec.md#shortcuts).
  $effect(() => registerAction('help.toggle', () => void (ui.helpOpen = !ui.helpOpen)));

  // The hash is the only route (ADR 0004): follow it, and keep the window size for the region limits.
  $effect(() => router.listen());
  $effect(() => {
    const sync = () => ui.setViewport({ width: window.innerWidth, height: window.innerHeight });
    sync();
    window.addEventListener('resize', sync);
    return () => window.removeEventListener('resize', sync);
  });

  // Opening: #/p/<id> loads that project, again after a reload, and again when the id changes.
  const route = $derived(router.current);
  $effect(() => {
    if (route.view === 'editor') {
      if (project.id !== route.id) void project.open(route.id);
    } else if (project.id !== null) {
      project.close(); // leaving the editor: the next open reads the project again
    }
  });
</script>

{#if route.view === 'editor'}
  {#if project.status === 'ready' && project.id === route.id}
    <EditorShell />
  {:else}
    <ProjectState />
  {/if}
{:else if route.view === 'library'}
  <LibraryView />
{:else}
  <StartView />
{/if}

<!-- Overlays sit outside the views so they never change the region boxes. -->
<ShortcutHelp />
<ToastHost />
