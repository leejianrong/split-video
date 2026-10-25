import { mount } from 'svelte';
import App from './App.svelte';
import { installShortcuts } from './lib/a11y/dispatcher';
import './lib/styles/tokens.css';
import './lib/styles/base.css';

const target = document.getElementById('app');
if (!target) throw new Error('missing #app mount point');

installShortcuts();

// The components gallery lives at /app/#/dev. It is not linked from the shell, and it is a separate chunk, so the
// normal page does not load it. (A project id will use the hash too, as #/p/<id>: docs/adr/0004.)
if (location.hash === '#/dev') {
  const { default: DevPage } = await import('./lib/dev/DevPage.svelte');
  mount(DevPage, { target });
} else {
  mount(App, { target });
}
