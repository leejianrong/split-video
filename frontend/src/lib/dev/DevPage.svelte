<script lang="ts">
  import { registerAction } from '../a11y/dispatcher';
  import { announce } from '../a11y/announce';
  import { ui } from '../state/ui.svelte';
  import { toasts } from '../state/toasts.svelte';
  import Button, { type ButtonSize, type ButtonVariant } from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import Dialog from '../ui/Dialog.svelte';
  import Icon, { ICONS, type IconName } from '../ui/icons/Icon.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Panel from '../ui/Panel.svelte';
  import Select from '../ui/Select.svelte';
  import ShortcutHelp from '../ui/ShortcutHelp.svelte';
  import Slider from '../ui/Slider.svelte';
  import Tabs from '../ui/Tabs.svelte';
  import TextField from '../ui/TextField.svelte';
  import ToastHost from '../ui/ToastHost.svelte';
  import Tooltip from '../ui/Tooltip.svelte';
  import { SHORTCUT_GROUPS, SHORTCUTS } from '../a11y/shortcuts';

  // The components gallery (SPL-61): every component, variant and state, at /app/#/dev. Hover and focus are the
  // real ones: point at a control or press Tab. Disabled is the real attribute.
  $effect(() => registerAction('help.toggle', () => void (ui.helpOpen = !ui.helpOpen)));

  const variants: ButtonVariant[] = ['primary', 'secondary', 'ghost', 'danger'];
  const sizes: ButtonSize[] = ['sm', 'md', 'lg'];
  const iconNames = Object.keys(ICONS) as IconName[];

  const tokenGroups: Record<string, string[]> = {
    Surfaces: ['bg', 'surface', 'surface-raised', 'surface-hover', 'surface-active'].map(
      (n) => `--color-${n}`,
    ),
    'Borders and text': ['border', 'border-strong', 'text', 'text-muted', 'text-disabled'].map(
      (n) => `--color-${n}`,
    ),
    Accent: [
      'accent',
      'accent-hover',
      'accent-active',
      'accent-text',
      'accent-subtle',
      'focus',
    ].map((n) => `--color-${n}`),
    Status: ['danger', 'danger-solid', 'danger-hover', 'danger-text', 'warn', 'ok'].map(
      (n) => `--color-${n}`,
    ),
  };
  const scaleTokens = [
    '--space-1',
    '--space-2',
    '--space-3',
    '--space-4',
    '--space-5',
    '--space-6',
    '--radius-sm',
    '--radius-md',
    '--radius-lg',
    '--font-size-xs',
    '--font-size-sm',
    '--font-size-base',
    '--font-size-md',
    '--font-size-lg',
    '--font-size-xl',
    '--z-dialog',
    '--z-toast',
    '--z-tooltip',
    '--size-topbar-height',
    '--size-tabrail-width',
    '--size-panel-width',
    '--size-inspector-width',
    '--size-timeline-height',
  ];

  let tokenValues = $state<Record<string, string>>({});
  $effect(() => {
    const style = getComputedStyle(document.documentElement);
    const all = [...Object.values(tokenGroups).flat(), ...scaleTokens];
    tokenValues = Object.fromEntries(all.map((t) => [t, style.getPropertyValue(t).trim()]));
  });

  let volume = $state(-6);
  let zoom = $state(2);
  let name = $state('My gig');
  let bad = $state('');
  let ratio = $state('16:9');
  let snapping = $state(true);
  let dialogOpen = $state(false);
  let tab = $state('media');
  let pressed = $state(false);
  let clicks = $state(0);

  const sections = [
    ['tokens', 'Tokens'],
    ['buttons', 'Buttons'],
    ['sliders', 'Sliders'],
    ['fields', 'Fields'],
    ['panels', 'Panels and tabs'],
    ['overlays', 'Dialog and toasts'],
    ['shortcuts', 'Shortcuts'],
  ];
</script>

<div class="dev">
  <header>
    <h1>Components</h1>
    <p>
      Design system gallery (SPL-61). Dark theme, tokens only. Press <kbd>?</kbd> for the shortcut list.
    </p>
    <nav aria-label="Sections">
      {#each sections as [id = '', label] (id)}<a
          href="#/dev"
          data-section={id}
          onclick={(e) => {
            e.preventDefault();
            document.getElementById(id)?.scrollIntoView();
          }}>{label}</a
        >{/each}
    </nav>
  </header>

  <main>
    <section id="tokens" aria-labelledby="h-tokens">
      <h2 id="h-tokens">Tokens</h2>
      <p class="note">
        Values are read from the live CSS custom properties (src/lib/styles/tokens.css).
      </p>
      {#each Object.entries(tokenGroups) as [group, names] (group)}
        <h3>{group}</h3>
        <table class="swatches">
          <thead
            ><tr><th scope="col">Swatch</th><th scope="col">Token</th><th scope="col">Value</th></tr
            ></thead
          >
          <tbody>
            {#each names as token (token)}
              <tr>
                <td><span class="swatch" style:background="var({token})"></span></td>
                <td><code>{token}</code></td>
                <td>{tokenValues[token]}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/each}
      <h3>Spacing, radii, type, layers, regions</h3>
      <table class="swatches">
        <thead><tr><th scope="col">Token</th><th scope="col">Value</th></tr></thead>
        <tbody>
          {#each scaleTokens as token (token)}
            <tr><td><code>{token}</code></td><td>{tokenValues[token]}</td></tr>
          {/each}
        </tbody>
      </table>
    </section>

    <section id="buttons" aria-labelledby="h-buttons">
      <h2 id="h-buttons">Buttons</h2>
      <p class="note">
        Hover and Tab to see the real hover and focus states. Disabled uses the real attribute.
      </p>
      <table class="matrix">
        <thead>
          <tr
            ><th scope="col">Variant</th>{#each sizes as s (s)}<th scope="col">{s}</th>{/each}<th
              scope="col">disabled</th
            ><th scope="col">with icon</th></tr
          >
        </thead>
        <tbody>
          {#each variants as variant (variant)}
            <tr>
              <th scope="row">{variant}</th>
              {#each sizes as size (size)}
                <td><Button {variant} {size} onclick={() => clicks++}>{variant}</Button></td>
              {/each}
              <td><Button {variant} disabled>{variant}</Button></td>
              <td><Button {variant}><Icon name="plus" /> Add</Button></td>
            </tr>
          {/each}
        </tbody>
      </table>
      <p>Clicks: <output data-testid="clicks">{clicks}</output></p>

      <h3>Icon buttons (label is the name and the tooltip; shortcut comes from the registry)</h3>
      <div class="row">
        <IconButton label="Play / pause" icon="play" action="transport.toggle" />
        <IconButton label="Split at the playhead" icon="scissors" action="timeline.split" />
        <IconButton label="Undo" icon="undo" action="history.undo" />
        <IconButton label="Redo" icon="redo" action="history.redo" />
        <IconButton label="Delete" icon="trash" action="timeline.delete" variant="secondary" />
        <IconButton label="Disabled" icon="close" disabled />
        <IconButton
          label="Snapping"
          icon="check"
          action="timeline.snapping"
          variant="secondary"
          aria-pressed={pressed}
          onclick={() => (pressed = !pressed)}
        />
        <IconButton label="Large" icon="help" size="lg" variant="primary" />
        <IconButton label="Small" icon="minus" size="sm" variant="secondary" />
      </div>

      <h3>Tooltip on any control</h3>
      <div class="row">
        <Tooltip label="Open Export" action="project.export"><Button>Export</Button></Tooltip>
      </div>

      <h3>Icons</h3>
      <ul class="icons">
        {#each iconNames as icon (icon)}<li>
            <Icon name={icon} size={24} /><span>{icon}</span>
          </li>{/each}
      </ul>
    </section>

    <section id="sliders" aria-labelledby="h-sliders">
      <h2 id="h-sliders">Sliders</h2>
      <p class="note">Arrows step, Home and End jump, Page Up and Page Down take a larger step.</p>
      <div class="stack narrow">
        <Slider label="Volume" bind:value={volume} min={-60} max={12} format={(v) => `${v} dB`} />
        <Slider label="Zoom" bind:value={zoom} min={1} max={10} step={1} format={(v) => `${v}x`} />
        <Slider label="Disabled" value={30} disabled />
      </div>
    </section>

    <section id="fields" aria-labelledby="h-fields">
      <h2 id="h-fields">Fields</h2>
      <div class="stack narrow">
        <TextField label="Project name" bind:value={name} hint="Used for the exported file name." />
        <TextField
          label="Save to"
          bind:value={bad}
          error="That folder does not exist. Fix: choose a folder that exists."
        />
        <TextField label="Disabled" value="read only" disabled />
        <Select
          label="Aspect ratio"
          bind:value={ratio}
          options={[
            { value: '16:9', label: '16:9' },
            { value: '9:16', label: '9:16' },
            { value: '1:1', label: '1:1' },
          ]}
        />
        <Checkbox label="Snapping on" bind:checked={snapping} />
        <Checkbox label="Disabled option" checked={false} disabled />
      </div>
    </section>

    <section id="panels" aria-labelledby="h-panels">
      <h2 id="h-panels">Panels and tabs</h2>
      <div class="two">
        <Panel title="Panel with actions" level={3}>
          {#snippet actions()}<Button size="sm" variant="primary">Import</Button>{/snippet}
          <p class="plain">Panels are titled frames; the title is the section's accessible name.</p>
        </Panel>
        <Panel title="Plain panel" level={3}><p class="plain">No actions.</p></Panel>
      </div>
      <h3>Tabs (roving tabindex: Tab enters the selected tab, arrows move, Home and End jump)</h3>
      <Tabs
        label="Library"
        bind:selected={tab}
        tabs={[
          { id: 'media', label: 'Media' },
          { id: 'highlights', label: 'Highlights' },
          { id: 'later', label: 'Effects', disabled: true },
          { id: 'transcript', label: 'Transcript' },
        ]}
      >
        {#snippet panel(id)}<p class="plain">Panel for <strong>{id}</strong>.</p>{/snippet}
      </Tabs>
    </section>

    <section id="overlays" aria-labelledby="h-overlays">
      <h2 id="h-overlays">Dialog and toasts</h2>
      <div class="row">
        <Button variant="primary" onclick={() => (dialogOpen = true)}>Open dialog</Button>
        <Button onclick={() => (ui.helpOpen = true)}>Open shortcut list</Button>
      </div>
      <h3>Toasts</h3>
      <div class="row">
        <Button onclick={() => toasts.show({ message: 'Saved just now.' })}>Info</Button>
        <Button
          onclick={() =>
            toasts.show({
              kind: 'success',
              message: 'Export finished: 4 files.',
              action: { label: 'Open folder', onclick: () => announce('Opened the folder') },
            })}>Success with action</Button
        >
        <Button
          onclick={() => toasts.show({ kind: 'warning', message: 'Preview is still preparing.' })}
          >Warning</Button
        >
        <Button
          variant="danger"
          onclick={() =>
            toasts.error('my-gig.mp4: out of disk space. Fix: free some space, then retry.', {
              label: 'Retry',
              onclick: () => {},
            })}>Error (stays)</Button
        >
        <Button variant="ghost" onclick={() => toasts.clear()}>Clear all</Button>
      </div>
      <p class="note">
        Toasts pause while the pointer or focus is on them. Errors stay until dismissed.
      </p>
    </section>

    <section id="shortcuts" aria-labelledby="h-shortcuts">
      <h2 id="h-shortcuts">Shortcuts</h2>
      <p class="note">
        Defined once in src/lib/a11y/shortcuts.ts; the same list as <code>?</code> and
        docs/ux-spec.md#shortcuts.
        {SHORTCUTS.length} entries in {SHORTCUT_GROUPS.length} groups.
      </p>
    </section>
  </main>
</div>

<Dialog
  bind:open={dialogOpen}
  title="Rename project"
  description="A dialog traps focus, closes on Escape and returns focus to its opener."
>
  <TextField label="Project name" bind:value={name} />
  {#snippet footer()}
    <Button onclick={() => (dialogOpen = false)}>Cancel</Button>
    <Button variant="primary" onclick={() => (dialogOpen = false)}>Save</Button>
  {/snippet}
</Dialog>
<ShortcutHelp />
<ToastHost />

<style>
  .dev {
    height: 100vh;
    overflow: auto;
    padding: var(--space-5);
  }

  header {
    max-width: 960px;
    margin: 0 auto var(--space-5);
  }

  header p {
    margin: var(--space-2) 0;
    color: var(--color-text-muted);
  }

  nav {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
  }

  a {
    color: var(--color-accent);
  }

  main {
    display: grid;
    gap: var(--space-6);
    max-width: 960px;
    margin: 0 auto;
  }

  section {
    padding: var(--space-4);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    background: var(--color-surface);
  }

  h1 {
    font-size: var(--font-size-xl);
  }
  h2 {
    margin-bottom: var(--space-3);
    font-size: var(--font-size-lg);
  }
  h3 {
    margin: var(--space-4) 0 var(--space-2);
    color: var(--color-text-muted);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
  }

  .note {
    margin: 0 0 var(--space-3);
    color: var(--color-text-muted);
  }
  .plain {
    margin: 0;
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    padding-bottom: var(--space-4);
  }
  .stack {
    display: grid;
    gap: var(--space-4);
  }
  .narrow {
    max-width: 360px;
  }
  .two {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-4);
  }

  table {
    border-collapse: collapse;
  }
  th,
  td {
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--color-border);
    text-align: left;
    vertical-align: middle;
  }
  th {
    color: var(--color-text-muted);
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-sm);
  }
  .swatch {
    display: block;
    width: 48px;
    height: 24px;
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-sm);
  }
  code {
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
  }

  .icons {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .icons li {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-1);
    width: 72px;
    font-size: var(--font-size-xs);
    color: var(--color-text-muted);
  }
</style>
