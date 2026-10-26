<script lang="ts">
  import { hrefTo } from '../state/route';
  import { project } from '../state/project.svelte';
  import Button from '../ui/Button.svelte';
  import PageFrame from './PageFrame.svelte';

  // What the editor route shows while the project is not ready: opening, or why it could not be opened.
  const notFound = $derived(project.error?.status === 404);
</script>

<PageFrame title={project.id ?? ''}>
  {#if project.status === 'error' && project.error}
    <div class="box" role="alert" data-testid="project-error" data-code={project.error.code}>
      <h1>{notFound ? 'This project was not found' : 'This project could not be opened'}</h1>
      <p>{project.error.message}</p>
      {#if project.error.fix}
        <p class="fix">{project.error.fix}</p>
      {:else if notFound}
        <p class="fix">
          Fix: it may have been moved or renamed. Pick the video again from the library.
        </p>
      {/if}
      <p class="muted">Project: <code>{project.id}</code></p>
      <div class="actions">
        <a class="back" href={hrefTo({ view: 'library', dir: '' })}>Back to the library</a>
        <Button onclick={() => project.id && project.open(project.id)}>Try again</Button>
      </div>
    </div>
  {:else}
    <p class="loading" role="status" data-testid="project-loading">Opening the project…</p>
  {/if}
</PageFrame>

<style>
  .box {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    max-width: 560px;
    margin: var(--space-7) auto;
    padding: var(--space-5);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-lg);
    background: var(--color-surface);
  }

  h1 {
    font-size: var(--font-size-xl);
  }

  p {
    margin: 0;
  }

  .muted {
    color: var(--color-text-muted);
    overflow-wrap: anywhere;
  }

  .fix {
    color: var(--color-text-muted);
  }

  .actions {
    display: flex;
    align-items: center;
    gap: var(--space-4);
  }

  .back {
    color: var(--color-accent);
  }

  .loading {
    margin: var(--space-7) 0;
    color: var(--color-text-muted);
    text-align: center;
  }
</style>
