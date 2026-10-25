import { assets } from './assets.svelte';
import { jobs } from './jobs.svelte';
import { project } from './project.svelte';

/**
 * Wire the stores that mirror the server to the one event stream (ADR 0004, "State"). Call once from the editor shell;
 * it returns the function that undoes it. The jobs store owns the connection; a `project` event moves the project
 * (agent and CLI edits show live), and a reconnect refetches what the page mirrors, because frames sent while the
 * stream was down are gone.
 */
export function connectLive(): () => void {
  const offProject = jobs.onProject((event) => project.handleProjectEvent(event));
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
