import { announce } from '../a11y/announce';
import { assets } from './assets.svelte';
import { clipSelection } from './clipSelection.svelte';
import { jobs, type ProjectEvent } from './jobs.svelte';
import { project } from './project.svelte';
import { toasts } from './toasts.svelte';

/** The clip ids on every track of the mirrored timeline. */
function clipIds(): Set<string> {
  return new Set(project.timeline?.tracks.flatMap((t) => t.clips.map((c) => c.id)) ?? []);
}

// One notice at a time: a burst of agent edits replaces it instead of stacking a toast per edit.
let notice: number | null = null;

/**
 * Someone else (an agent through the CLI or MCP, another tab) changed the open project: show the new version and say
 * so. The selection keeps the clips that still exist; if any selected clip is gone the notice says that too.
 */
async function onProjectEvent(event: ProjectEvent): Promise<void> {
  const selected = [...clipSelection.ids];
  if (!(await project.handleProjectEvent(event))) return;
  const ids = clipIds();
  const lost = selected.filter((id) => !ids.has(id)).length;
  clipSelection.prune(ids);
  let message =
    'The project was changed by another tool. The timeline now shows the latest version.';
  if (lost > 0) {
    message +=
      lost === 1
        ? ' The clip you selected was removed.'
        : ` ${lost} of the clips you selected were removed.`;
  }
  if (notice !== null) toasts.dismiss(notice);
  notice = toasts.show({ kind: 'info', message });
  announce(message);
}

/**
 * Wire the stores that mirror the server to the one event stream (ADR 0004, "State"). Call once from the editor shell;
 * it returns the function that undoes it. The jobs store owns the connection; a `project` event moves the project
 * (agent and CLI edits show live), and a reconnect refetches what the page mirrors, because frames sent while the
 * stream was down are gone.
 */
export function connectLive(): () => void {
  const offProject = jobs.onProject((event) => void onProjectEvent(event));
  const offResync = jobs.onResync(() => {
    void project.refresh();
    void assets.load();
  });
  jobs.start();
  return () => {
    offProject();
    offResync();
    jobs.stop();
  };
}
