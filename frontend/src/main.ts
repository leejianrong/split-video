import { mount } from 'svelte';
import App from './App.svelte';
import { installShortcuts } from './lib/a11y/dispatcher';
import { parseHash } from './lib/state/route';
import './lib/styles/tokens.css';
import './lib/styles/base.css';

const target = document.getElementById('app');
if (!target) throw new Error('missing #app mount point');

installShortcuts();

// The components gallery lives at /app/#/dev. It is not linked from the shell, and it is a separate chunk, so the
// normal page does not load it. The other views and the open project use the hash too (lib/state/route.ts).
if (parseHash(location.hash).view === 'dev') {
  const { default: DevPage } = await import('./lib/dev/DevPage.svelte');
  mount(DevPage, { target });
} else {
  mount(App, { target });
}
