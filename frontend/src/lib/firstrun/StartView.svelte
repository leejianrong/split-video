<script lang="ts">
  import { onMount } from 'svelte';
  import { getSessionApiV1SessionGet, type SessionResponse } from '../api/client';
  import { baseName, formatLength } from '../format';
  import { hasFileDrag } from '../state/dnd';
  import { assets } from '../state/assets.svelte';
  import { firstRun, GOALS, goalById, type Step } from '../state/firstrun.svelte';
  import { connectLive } from '../state/live';
  import { hrefTo } from '../state/route';
  import { router } from '../state/route.svelte';
  import { ui } from '../state/ui.svelte';
  import PageFrame from '../shell/PageFrame.svelte';
  import PickDialog from '../tabs/media/PickDialog.svelte';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import Icon from '../ui/icons/Icon.svelte';
  import ProgressBar from '../ui/ProgressBar.svelte';

  // The first-run page (docs/ux-spec.md#first-run, wireframe board 3): add a recording, say what to make, watch the
  // preparation, open the editor. The goal runs the Media tab's quick action; nothing here edits the project.
  let input: HTMLInputElement | undefined = $state();
  let pickOpen = $state(false);
  let dragging = $state(false);
  let depth = 0;

  // The server's job events drive the steps; the editor opens the same stream when it mounts.
  $effect(() => connectLive());
  // Coming back to this page (the logo link) starts a new project.
  onMount(() => firstRun.reset());

  // When the server was started on one video, offer it directly (GET /api/v1/session).
  let session = $state<SessionResponse | null>(null);
  $effect(() => {
    getSessionApiV1SessionGet().then(
      ({ data }) => (session = data),
      () => (session = null),
    );
  });

  const upload = $derived(assets.uploads[0] ?? null);
  const asset = $derived(firstRun.current);
  const steps = $derived(firstRun.steps);
  const goal = $derived(firstRun.goal ? goalById(firstRun.goal) : null);
  let adding = $state<string | null>(null);
  let addError = $state<string | null>(null);

  function pick(files: File[]) {
    if (files.length === 0) return;
    firstRun.clear();
    assets.enqueue(files.slice(0, 1), (a) => firstRun.setAsset(a));
  }

  function choose(event: Event) {
    const target = event.currentTarget as HTMLInputElement;
    pick(Array.from(target.files ?? []));
    target.value = '';
  }

  function onDragOver(event: DragEvent) {
    if (!hasFileDrag(event.dataTransfer)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  }
  function onDragEnter(event: DragEvent) {
    if (!hasFileDrag(event.dataTransfer)) return;
    event.preventDefault();
    depth += 1;
    dragging = true;
  }
  function onDragLeave() {
    depth = Math.max(0, depth - 1);
    if (depth === 0) dragging = false;
  }
  function onDrop(event: DragEvent) {
    if (!hasFileDrag(event.dataTransfer)) return;
    event.preventDefault();
    depth = 0;
    dragging = false;
    pick(Array.from(event.dataTransfer?.files ?? []));
  }

  async function useExisting(path: string) {
    adding = path;
    addError = null;
    try {
      firstRun.clear();
      firstRun.setAsset(await assets.register(path));
    } catch {
      addError =
        'That file could not be added. Fix: check that it is a video in the recordings folder.';
    } finally {
      adding = null;
    }
  }

  function openEditor(skip: boolean) {
    const id = asset?.path;
    if (!id) return;
    ui.setTab(!skip && goal ? goal.tab : 'media');
    router.go({ view: 'editor', id });
  }

  const stateText: Record<Step['state'], string> = {
    waiting: 'Waiting',
    working: 'Working',
    done: 'Done',
    failed: 'Failed',
  };
</script>

<PageFrame title="New project" note="Everything stays on this computer">
  <div class="columns">
    <section class="col" aria-labelledby="step1">
      <h1 id="step1">1. Add your recording</h1>
      <!-- A drop target is not a control: the keyboard way in is the Choose file button. -->
      <div
        class="drop"
        class:dragging
        data-testid="drop-zone"
        role="group"
        aria-label="Add your recording"
        ondragenter={onDragEnter}
        ondragover={onDragOver}
        ondragleave={onDragLeave}
        ondrop={onDrop}
      >
        {#if asset}
          <Icon name="video" size={44} />
          <p class="big" data-testid="chosen-name">{baseName(asset.path)}</p>
          <p class="muted" data-testid="chosen-length">{formatLength(asset.duration)}</p>
          {#if !firstRun.started}
            <Button variant="ghost" onclick={() => firstRun.clear()}>Choose another file</Button>
          {/if}
        {:else if upload}
          <Icon name={upload.status === 'failed' ? 'alert' : 'upload'} size={44} />
          <p class="big">{upload.name}</p>
          {#if upload.status === 'failed' && upload.error}
            <p class="error" role="alert" data-testid="upload-error">
              {upload.error.message}
              {#if upload.error.fix}<br />Fix: {upload.error.fix}{/if}
            </p>
            <div class="actions">
              <Button onclick={() => assets.retry(upload.id)}>Retry</Button>
              <Button variant="ghost" onclick={() => assets.cancelUpload(upload.id)}>Dismiss</Button
              >
            </div>
          {:else}
            <p class="muted" data-testid="importing">
              {upload.status === 'queued'
                ? 'Waiting to import'
                : `Importing ${Math.round((upload.size ? upload.loaded / upload.size : 0) * 100)}%`}
            </p>
            <div class="wide">
              <ProgressBar
                value={upload.status === 'queued' || !upload.size ? 0 : upload.loaded / upload.size}
                label={`Importing ${upload.name}`}
              />
            </div>
            <Button variant="ghost" onclick={() => assets.cancelUpload(upload.id)}>Cancel</Button>
          {/if}
        {:else}
          <Icon name="upload" size={44} />
          <p class="big">Drop a video here</p>
          <p class="muted">
            Or choose a file from your recordings folder. Streams, gigs and lectures of any length
            work.
          </p>
          <div class="actions">
            <Button
              variant="primary"
              size="lg"
              data-testid="choose-file"
              onclick={() => input?.click()}>Choose file</Button
            >
            <Button size="lg" data-testid="choose-folder" onclick={() => (pickOpen = true)}
              >From the recordings folder</Button
            >
            {#if session?.file_open && session.path}
              <Button
                size="lg"
                data-testid="open-session"
                disabled={adding !== null}
                onclick={() => useExisting(session?.path ?? '')}
                >Use {session.filename ?? session.path}</Button
              >
            {/if}
          </div>
          {#if addError}<p class="error" role="alert">{addError}</p>{/if}
        {/if}
        <input
          bind:this={input}
          class="file-input"
          type="file"
          accept="video/*,audio/*,.mkv,.ts,.m2ts"
          tabindex="-1"
          aria-hidden="true"
          data-testid="file-input"
          onchange={choose}
        />
      </div>
      <p class="muted safe"><Icon name="check" size={14} /> Everything stays on this computer.</p>
      <a class="plain" href={hrefTo({ view: 'library', dir: '' })} data-testid="open-library"
        >Browse the library instead</a
      >
    </section>

    <section class="col" aria-labelledby="step2">
      <h2 id="step2">2. What do you want to make?</h2>
      <ul class="goals">
        {#each GOALS as g (g.id)}
          <li>
            <button
              type="button"
              class="goal"
              data-testid={`goal-${g.id}`}
              aria-pressed={firstRun.goal === g.id}
              disabled={firstRun.started && firstRun.goal !== g.id}
              onclick={() => firstRun.choose(g.id)}
            >
              <span class="icon">
                <Icon
                  name={g.id === 'songs'
                    ? 'scissors'
                    : g.id === 'empty'
                      ? 'plus'
                      : g.id === 'shorts'
                        ? 'video'
                        : 'highlights'}
                  size={22}
                />
              </span>
              <span class="text">
                <span class="title">{g.title}</span>
                <span class="muted">{g.text}</span>
              </span>
            </button>
          </li>
        {/each}
      </ul>
      {#if asset && !firstRun.started && firstRun.goal !== 'shorts'}
        <!-- Transcription is optional: it needs the add-on, and fails with a fix hint without it. -->
        <Checkbox label="Also transcribe the speech" bind:checked={firstRun.transcribe} />
      {/if}
      {#if !asset}
        <p class="muted">
          Pick a goal now or after the recording is added. It starts as soon as both are set.
        </p>
      {/if}

      {#if steps.length > 0}
        <ul class="steps" aria-label="Preparing" data-testid="steps">
          {#each steps as step (step.id)}
            <li class="step" data-testid={`step-${step.id}`} data-state={step.state}>
              <span class="mark" data-state={step.state}>
                {#if step.state === 'done'}<Icon name="check" size={14} />
                {:else if step.state === 'failed'}<Icon name="alert" size={14} />{/if}
              </span>
              <span class="body">
                <span>
                  {step.label}
                  {#if step.state === 'working' && step.fraction !== null}
                    {Math.round(step.fraction * 100)}%
                  {/if}
                </span>
                <span class="muted">
                  {stateText[step.state]}{step.summary ? `: ${step.summary}` : ''}
                </span>
                {#if step.state === 'working'}
                  <ProgressBar value={step.fraction} label={step.label} />
                {/if}
                {#if step.state === 'failed'}
                  <span class="error">
                    {step.error?.message ?? 'Cancelled.'}
                    {#if step.error?.fix}Fix: {step.error.fix}{/if}
                    {#if step.optional}You can carry on without it.{/if}
                  </span>
                  <span
                    ><Button size="sm" onclick={() => firstRun.retry(step.id)}>Retry</Button></span
                  >
                {/if}
              </span>
            </li>
          {/each}
        </ul>
      {/if}

      <div class="footer">
        <Button
          variant="primary"
          size="lg"
          data-testid="open-editor"
          disabled={!asset || !firstRun.canOpen}
          onclick={() => openEditor(false)}>Open the editor</Button
        >
        <Button
          variant="ghost"
          size="lg"
          data-testid="skip"
          disabled={!asset}
          onclick={() => openEditor(true)}>Skip, I'll choose later</Button
        >
      </div>
    </section>
  </div>
</PageFrame>

<PickDialog bind:open={pickOpen} onpick={(a) => firstRun.setAsset(a)} />

<style>
  .columns {
    display: flex;
    gap: var(--space-6);
    max-width: 1200px;
    margin: 0 auto;
  }

  .col {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    flex: 1;
    min-width: 0;
  }

  h1,
  h2 {
    font-size: var(--font-size-xl);
  }

  .drop {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--space-3);
    min-height: 360px;
    padding: var(--space-6);
    border: 2px dashed var(--color-border-strong);
    border-radius: var(--radius-lg);
    background: var(--color-surface);
    color: var(--color-accent);
    text-align: center;
  }
  .drop.dragging {
    border-color: var(--color-accent);
    background: var(--color-accent-subtle);
  }

  .drop p {
    margin: 0;
  }

  .big {
    color: var(--color-text);
    font-size: var(--font-size-xl);
    font-weight: var(--font-weight-bold);
    overflow-wrap: anywhere;
  }

  .muted {
    color: var(--color-text-muted);
  }

  .error {
    color: var(--color-danger);
  }

  .wide {
    width: 100%;
    max-width: 360px;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: var(--space-3);
  }

  .file-input {
    display: none;
  }

  .safe {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
  }

  .plain {
    color: var(--color-text-muted);
  }

  .goals,
  .steps {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .goal {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    width: 100%;
    padding: var(--space-4);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    background: var(--color-surface);
    color: var(--color-text);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .goal:hover:not(:disabled) {
    background: var(--color-surface-hover);
  }
  .goal[aria-pressed='true'] {
    border-color: var(--color-accent);
    background: var(--color-accent-subtle);
  }
  .goal:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }

  .text {
    display: flex;
    flex-direction: column;
  }

  .icon {
    display: grid;
    place-items: center;
    flex: none;
    width: 44px;
    height: 44px;
    border-radius: var(--radius-lg);
    background: var(--color-accent-subtle);
    color: var(--color-accent);
  }

  .title {
    font-weight: var(--font-weight-bold);
  }

  .steps {
    gap: var(--space-2);
  }

  .step {
    display: flex;
    gap: var(--space-3);
    align-items: flex-start;
  }

  .body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }

  .mark {
    display: grid;
    place-items: center;
    flex: none;
    width: 20px;
    height: 20px;
    margin-top: 2px;
    border: 2px solid var(--color-border-strong);
    border-radius: var(--radius-full);
  }
  .mark[data-state='done'] {
    border-color: var(--color-ok);
    color: var(--color-ok);
  }
  .mark[data-state='working'] {
    border-color: var(--color-accent);
  }
  .mark[data-state='failed'] {
    border-color: var(--color-danger);
    color: var(--color-danger);
  }

  .footer {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
  }
</style>
