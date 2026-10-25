import { mount } from 'svelte';
import App from './App.svelte';
import './lib/styles/tokens.css';
import './lib/styles/base.css';

const target = document.getElementById('app');
if (!target) throw new Error('missing #app mount point');

export default mount(App, { target });
