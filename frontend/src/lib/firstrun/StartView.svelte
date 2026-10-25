<script lang="ts">
  import { getSessionApiV1SessionGet, type SessionResponse } from '../api/client';
  import { hrefTo } from '../state/route';
  import PageFrame from '../shell/PageFrame.svelte';
  import Icon, { type IconName } from '../ui/icons/Icon.svelte';

  // The entry screen (docs/ux-spec.md#first-run, wireframe board 3): add a recording, then say what to make. Only
  // opening a video is wired; the goals are shown so the page reads as the first-run page, and are not choosable yet.
  const goals: { title: string; text: string; icon: IconName }[] = [
    {
      title: 'Split into songs',
      text: 'Finds the gaps between songs and cuts one clip per song.',
      icon: 'scissors',
    },
    {
      title: 'Find highlights',
      text: 'Suggests the best moments to review and keep.',
      icon: 'highlights',
    },
    {
      title: 'Make vertical shorts',
      text: 'Highlights cropped to 9:16 with captions, ready to post.',
      icon: 'video',
    },
    {
      title: 'Start with an empty timeline',
      text: 'Do it yourself: cut, trim and arrange clips.',
      icon: 'plus',
    },
  ];

  // When the server was started on one video, offer it directly (GET /api/v1/session).
  let session = $state<SessionResponse | null>(null);
  $effect(() => {
    getSessionApiV1SessionGet().then(
      ({ data }) => (session = data),
      () => (session = null),
    );
  });
</script>

<PageFrame title="New project" note="Everything stays on this computer">
  <div class="columns">
    <section class="col" aria-labelledby="step1">
      <h1 id="step1">1. Add your recording</h1>
      <div class="drop">
        <Icon name="upload" size={44} />
        <p class="big">Choose a video</p>
        <p class="muted">
          Pick a file from your recordings folder. Streams, gigs and lectures of any length work.
        </p>
        <div class="actions">
          <a class="cta" href={hrefTo({ view: 'library', dir: '' })} data-testid="open-library"
            >Open the library</a
          >
          {#if session?.file_open && session.path}
            <a
              class="cta secondary"
              href={hrefTo({ view: 'editor', id: session.path })}
              data-testid="open-session">Open {session.filename ?? session.path}</a
            >
          {/if}
        </div>
      </div>
    </section>

    <section class="col" aria-labelledby="step2">
      <h2 id="step2">2. What do you want to make?</h2>
      <p class="muted">You choose this after the video is open.</p>
      <ul class="goals">
        {#each goals as goal (goal.title)}
          <li class="goal">
            <span class="icon"><Icon name={goal.icon} size={22} /></span>
            <span>
              <span class="title">{goal.title}</span>
              <span class="muted">{goal.text}</span>
            </span>
          </li>
        {/each}
      </ul>
    </section>
  </div>
</PageFrame>

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

  .drop p {
    margin: 0;
  }

  .big {
    color: var(--color-text);
    font-size: var(--font-size-xl);
    font-weight: var(--font-weight-bold);
  }

  .muted {
    color: var(--color-text-muted);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: var(--space-3);
  }

  .cta {
    display: inline-flex;
    align-items: center;
    height: var(--control-height-lg);
    padding: 0 var(--space-4);
    border: 1px solid var(--color-accent);
    border-radius: var(--radius-md);
    background: var(--color-accent);
    color: var(--color-accent-text);
    font-weight: var(--font-weight-bold);
    text-decoration: none;
  }
  .cta:hover {
    background: var(--color-accent-hover);
  }
  .cta.secondary {
    background: var(--color-surface-raised);
    border-color: var(--color-border-strong);
    color: var(--color-text);
  }
  .cta.secondary:hover {
    background: var(--color-surface-hover);
  }

  .goals {
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
    padding: var(--space-4);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    background: var(--color-surface);
  }

  .goal > span:last-child {
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
</style>
