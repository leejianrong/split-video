<script lang="ts">
  import { exports } from '../state/exports.svelte';
  import { project } from '../state/project.svelte';
  import { ui } from '../state/ui.svelte';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import Dialog from '../ui/Dialog.svelte';
  import Select from '../ui/Select.svelte';
  import TextField from '../ui/TextField.svelte';
  import { defaultFolder, estimateSeconds, formatEstimate, MODES, PRESETS } from './plan';
  import RenderQueue from './RenderQueue.svelte';

  // The export dialog (ux-spec #export): what to export, the settings, then Start export (N). The render queue sits
  // beside it. Closing the dialog changes nothing about the renders: they run on the server's job queue.
  const s = exports.settings;
  const fixed = $state({ preset: 'vertical-1080', fit: 'crop' });
  const files = $derived(exports.files);
  const shorts = $derived(s.mode === 'shorts');
  const folderDefault = $derived(project.id ? defaultFolder(project.id) : '');
  const estimate = $derived(formatEstimate(estimateSeconds(files, s)));
  const summary = $derived(
    [
      PRESETS.find((p) => p.id === (shorts ? 'vertical-1080' : s.preset))?.label,
      s.captions ? 'captions' : '',
    ]
      .filter(Boolean)
      .join(', '),
  );
  const canStart = $derived(files.length > 0 && !exports.starting);
  const presets = $derived(
    s.mode === 'segments' ? PRESETS : PRESETS.filter((p) => p.id !== 'original'),
  );
  const emptyText = $derived(
    s.mode === 'shorts'
      ? 'No highlights on the timeline yet. Make a short in the Highlights tab first.'
      : s.mode === 'segments'
        ? 'No included segments on the timeline.'
        : 'The timeline is empty.',
  );

  // Switching away from the stream copy (only segments have it) must not leave a preset the mode cannot show.
  $effect(() => {
    if (s.mode !== 'segments' && s.preset === 'original') s.preset = 'youtube-1080p';
  });

  async function start() {
    await exports.start();
  }
</script>

<Dialog bind:open={ui.exportOpen} title="Export and render queue" width={920}>
  <div class="grid">
    <div class="settings">
      <fieldset class="modes">
        <legend>What to export</legend>
        {#each MODES as mode (mode.id)}
          <label class="mode" class:selected={s.mode === mode.id}>
            <input type="radio" name="export-mode" value={mode.id} bind:group={s.mode} />
            <span>
              <strong>{mode.label}</strong>
              <small>{mode.hint}</small>
            </span>
          </label>
        {/each}
      </fieldset>

      <div class="row">
        {#if shorts}
          <!-- Shorts are always vertical and cropped to fill: shown, not chosen. -->
          <Select
            label="Format"
            bind:value={fixed.preset}
            options={[{ value: 'vertical-1080', label: 'Vertical 1080x1920' }]}
            disabled
            data-testid="export-preset"
          />
          <Select
            label="If the shape differs"
            bind:value={fixed.fit}
            options={[{ value: 'crop', label: 'Crop to fill' }]}
            disabled
            data-testid="export-fit"
          />
        {:else}
          <Select
            label="Format"
            bind:value={s.preset}
            options={presets.map((p) => ({ value: p.id, label: p.label }))}
            data-testid="export-preset"
          />
          <Select
            label="If the shape differs"
            bind:value={s.fit}
            options={[
              { value: 'crop', label: 'Crop to fill' },
              { value: 'letterbox', label: 'Letterbox' },
            ]}
            disabled={s.preset === 'original'}
            data-testid="export-fit"
          />
        {/if}
      </div>

      <Checkbox
        label="Burn in captions (Shorts style)"
        bind:checked={s.captions}
        disabled={s.preset === 'original' && !shorts}
        data-testid="export-captions"
      />
      <p class="note">
        Captions come from the recording's transcript; without one the file has none.
      </p>

      <TextField
        label="Save to"
        bind:value={s.folder}
        placeholder={folderDefault}
        hint="A folder in the library. Empty uses the folder next to the recording."
        data-testid="export-folder"
      />
      <Checkbox label="Replace files that already exist" bind:checked={s.overwrite} />

      <p class="estimate" data-testid="export-estimate">
        {#if files.length > 0}
          Estimated time: {estimate}, up to 2 renders at once.
        {:else}
          {emptyText}
        {/if}
      </p>
      <p class="note">Nothing leaves this machine.</p>

      {#if exports.error}
        <p class="problem" role="alert" data-testid="export-error">
          {exports.error.message}
          {#if exports.error.fix}<strong>Fix:</strong> {exports.error.fix}{/if}
        </p>
      {/if}
    </div>

    <RenderQueue />
  </div>

  {#snippet footer()}
    <span class="summary">
      {summary}
    </span>
    <Button onclick={() => (ui.exportOpen = false)}>Close</Button>
    <Button variant="primary" disabled={!canStart} onclick={start} data-testid="start-export">
      Start export ({files.length})
    </Button>
  {/snippet}
</Dialog>

<style>
  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: var(--space-5);
    /* Each column scrolls on its own, so the queue stays in view while the settings are long (720 px windows). */
    height: min(560px, calc(100vh - 190px));
    min-height: 0;
  }
  .grid > :global(*) {
    overflow: auto;
    min-height: 0;
    padding-right: var(--space-1);
  }
  .row :global(select) {
    width: 100%;
  }
  .settings {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
  }
  fieldset {
    margin: 0;
    padding: 0;
    border: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  legend {
    padding: 0;
    margin-bottom: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--color-text-muted);
  }
  .mode {
    display: flex;
    gap: var(--space-2);
    align-items: flex-start;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    cursor: pointer;
  }
  .mode.selected {
    border-color: var(--color-accent);
    background: var(--color-accent-subtle);
  }
  .mode small {
    display: block;
    color: var(--color-text-muted);
  }
  .mode input {
    margin-top: 3px;
    accent-color: var(--color-accent);
  }
  .row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: var(--space-3);
  }
  .note,
  .estimate {
    margin: 0;
    color: var(--color-text-muted);
    font-size: var(--font-size-sm);
  }
  .estimate {
    color: var(--color-text);
  }
  .problem {
    margin: 0;
    color: var(--color-danger);
  }
  .problem strong {
    color: var(--color-text);
  }
  .summary {
    margin-right: auto;
    align-self: center;
    color: var(--color-text-muted);
    font-size: var(--font-size-sm);
  }
  @media (max-width: 1100px) {
    .grid {
      grid-template-columns: 1fr;
    }
  }
</style>
