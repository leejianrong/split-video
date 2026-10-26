<script lang="ts">
  import type { ClipOut, TrackOut } from '../api/client';
  import { announce } from '../a11y/announce';
  import { assets } from '../state/assets.svelte';
  import { project } from '../state/project.svelte';
  import { toasts } from '../state/toasts.svelte';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import Select from '../ui/Select.svelte';
  import Tabs from '../ui/Tabs.svelte';
  import TextField from '../ui/TextField.svelte';
  import ClipSlider from './ClipSlider.svelte';
  import NumberField from './NumberField.svelte';
  import {
    CLIP_DEFAULTS,
    VOLUME_DB,
    dbToGain,
    gainToDb,
    matchPreset,
    presetCrop,
    propertyOps,
    withCropField,
    type ClipProperties,
    type Crop,
    type CropPreset,
  } from './fields';

  // The contextual inspector for the selected clip(s) (ux-spec #inspector). It holds no rule: every control is one
  // `set_clip_properties` / `set_text_properties` op sent through `project.applyOps` (If-Match, 409 handling), and the
  // values shown are always the engine's, read back from `project.timeline`.
  type Entry = { clip: ClipOut; track: TrackOut };
  let { entries }: { entries: Entry[] } = $props();

  const first = $derived(entries[0]!);
  const clip = $derived(first.clip);
  const ids = $derived(entries.map((e) => e.clip.id));
  const many = $derived(entries.length > 1);
  const allText = $derived(entries.every((e) => e.clip.kind === 'text'));
  const anyText = $derived(entries.some((e) => e.clip.kind === 'text'));
  const allAudioTrack = $derived(entries.every((e) => e.track.kind === 'audio'));
  const allOverlay = $derived(
    entries.every((e) => e.track.kind === 'video' && !e.track.magnetic && e.clip.kind !== 'text'),
  );
  const onMain = $derived(first.track.kind === 'video' && first.track.magnetic);
  const locked = $derived(entries.some((e) => e.track.locked));

  type TabId = 'video' | 'audio' | 'speed' | 'crop' | 'captions';
  const tabs = $derived.by(() => {
    if (anyText) return [];
    if (allAudioTrack) return [{ id: 'audio', label: 'Audio' }];
    if (many) {
      return [
        ...(allOverlay ? [{ id: 'video', label: 'Video' }] : []),
        { id: 'audio', label: 'Audio' },
      ];
    }
    return [
      { id: 'video', label: 'Video' },
      { id: 'audio', label: 'Audio' },
      { id: 'speed', label: 'Speed' },
      { id: 'crop', label: 'Crop' },
      { id: 'captions', label: 'Captions' },
    ];
  });
  let chosen = $state<string>('video');
  const selected = $derived(tabs.some((t) => t.id === chosen) ? chosen : (tabs[0]?.id ?? 'video'));

  async function setProps(properties: ClipProperties, what: string) {
    const ok = await project.applyOps(propertyOps(ids, properties));
    done(ok, what);
  }

  async function setText(changes: Record<string, unknown>, what: string) {
    const ok = await project.applyOps(
      ids.map((id) => ({ op: 'set_text_properties', clip: id, ...changes })),
    );
    done(ok, what);
  }

  function done(ok: boolean, what: string) {
    if (ok) announce(`${what} changed`);
    else if (project.editError) {
      toasts.error(`${project.editError.message} ${project.editError.fix}`.trim());
    }
  }

  const percent = (v: number) => `${Math.round(v * 100)}%`;
  const db = (v: number) =>
    v <= VOLUME_DB.min ? 'Silent' : `${v > 0 ? '+' : ''}${v.toFixed(1)} dB`;
  const asset = $derived(assets.byId(clip.asset || null));
  const sourceW = $derived(asset?.width ?? null);
  const sourceH = $derived(asset?.height ?? null);
  const crop = $derived<Crop | null>(clip.crop ?? null);
  const activePreset = $derived(matchPreset(crop, sourceW, sourceH));
  let cropError = $state('');

  function applyPreset(preset: CropPreset) {
    cropError = '';
    const next = presetCrop(preset, sourceW, sourceH);
    // The source's own shape needs no crop: remove it.
    void setProps({ crop: next }, `Crop ${preset}`);
  }

  function setCropField(field: keyof Crop, percentValue: number) {
    const next = withCropField(crop, field, percentValue);
    if (!next.ok) {
      cropError = next.error;
      return;
    }
    cropError = '';
    void setProps({ crop: next.crop }, 'Crop region');
  }

  const cropRule = (name: string) => ({ name, min: 0, max: 100, unit: '%' });
  const changed = $derived(
    clip.gain !== 1 ||
      clip.muted ||
      clip.brightness !== 0 ||
      clip.contrast !== 1 ||
      clip.saturation !== 1 ||
      clip.opacity !== 1 ||
      clip.scale !== 1 ||
      clip.x !== 0.5 ||
      clip.y !== 0.5 ||
      crop !== null,
  );
  const POSITIONS = [
    { value: 'top', label: 'Top' },
    { value: 'middle', label: 'Middle' },
    { value: 'bottom', label: 'Bottom' },
  ];
</script>

<div class="clip-inspector" data-testid="clip-inspector">
  {#if locked}
    <p class="note" role="status">
      This lane is locked. Fix: unlock it in the timeline to edit the clip.
    </p>
  {/if}

  {#if allText && !many}
    <section class="group" aria-label="Title settings" data-testid="inspector-text">
      <TextField
        label="Text"
        value={clip.text}
        disabled={locked}
        onchange={(e) => void setText({ text: e.currentTarget.value }, 'Text')}
      />
      <NumberField
        label="Size (% of the frame height)"
        value={clip.font_size}
        rule={{ name: 'Size', min: 1, max: 50, unit: '%' }}
        toDisplay={(v) => v * 100}
        fromDisplay={(v) => v / 100}
        disabled={locked}
        oncommit={(v) => void setText({ font_size: v }, 'Size')}
      />
      <div class="field">
        <label for="inspector-text-color">Colour</label>
        <input
          id="inspector-text-color"
          type="color"
          value={clip.color}
          disabled={locked}
          onchange={(e) => void setText({ color: e.currentTarget.value }, 'Colour')}
        />
      </div>
      <div class="row">
        <NumberField
          label="X (%)"
          value={clip.x}
          rule={cropRule('X')}
          toDisplay={(v) => v * 100}
          fromDisplay={(v) => v / 100}
          disabled={locked}
          oncommit={(v) => void setProps({ x: v }, 'Position')}
        />
        <NumberField
          label="Y (%)"
          value={clip.y}
          rule={cropRule('Y')}
          toDisplay={(v) => v * 100}
          fromDisplay={(v) => v / 100}
          disabled={locked}
          oncommit={(v) => void setProps({ y: v }, 'Position')}
        />
      </div>
    </section>
  {:else if anyText}
    <p class="note" data-testid="inspector-mixed">
      Several clips are selected, and not all of them are the same kind. Fix: select clips of one
      kind to edit them together.
    </p>
  {:else}
    <Tabs
      compact
      {tabs}
      bind:selected={() => selected, (id: string) => (chosen = id)}
      label="Clip settings"
    >
      {#snippet panel(id)}
        <div class="group" data-testid="inspector-{id}">
          {#if (id as TabId) === 'video'}
            {#if !many}
              <Select
                label="Canvas fit"
                value="letterbox"
                disabled
                options={[{ value: 'letterbox', label: 'Letterbox' }]}
              />
              <p class="hint">
                Arrives with a per-clip fit edit. The export's fit (Crop to fill or Letterbox)
                applies to every clip for now.
              </p>
            {/if}
            {#if onMain}
              <p class="hint" data-testid="main-note">
                Position, scale and opacity apply to clips on an overlay lane; clips on the main
                track fill the frame. Use Crop to reframe this clip.
              </p>
            {/if}
            <ClipSlider
              label="Scale"
              value={clip.scale}
              min={0.05}
              max={4}
              step={0.05}
              format={percent}
              disabled={locked || onMain}
              oncommit={(v) => void setProps({ scale: v }, 'Scale')}
            />
            <div class="row">
              <NumberField
                label="Position X (%)"
                value={clip.x}
                rule={cropRule('Position X')}
                toDisplay={(v) => v * 100}
                fromDisplay={(v) => v / 100}
                disabled={locked || onMain}
                oncommit={(v) => void setProps({ x: v }, 'Position')}
              />
              <NumberField
                label="Position Y (%)"
                value={clip.y}
                rule={cropRule('Position Y')}
                toDisplay={(v) => v * 100}
                fromDisplay={(v) => v / 100}
                disabled={locked || onMain}
                oncommit={(v) => void setProps({ y: v }, 'Position')}
              />
            </div>
            <ClipSlider
              label="Opacity"
              value={clip.opacity}
              min={0}
              max={1}
              step={0.05}
              format={percent}
              disabled={locked || onMain}
              oncommit={(v) => void setProps({ opacity: v }, 'Opacity')}
            />
            {#if !many}
              <ClipSlider
                label="Brightness"
                value={clip.brightness}
                min={-1}
                max={1}
                step={0.05}
                format={(v) => v.toFixed(2)}
                disabled={locked}
                oncommit={(v) => void setProps({ brightness: v }, 'Brightness')}
              />
              <ClipSlider
                label="Contrast"
                value={clip.contrast}
                min={0}
                max={3}
                step={0.05}
                format={(v) => v.toFixed(2)}
                disabled={locked}
                oncommit={(v) => void setProps({ contrast: v }, 'Contrast')}
              />
              <ClipSlider
                label="Saturation"
                value={clip.saturation}
                min={0}
                max={3}
                step={0.05}
                format={(v) => v.toFixed(2)}
                disabled={locked}
                oncommit={(v) => void setProps({ saturation: v }, 'Saturation')}
              />
              <Button
                variant="secondary"
                size="sm"
                disabled={locked || !changed}
                onclick={() => void setProps({ ...CLIP_DEFAULTS }, 'Clip settings')}
              >
                Reset clip
              </Button>
            {/if}
          {:else if (id as TabId) === 'audio'}
            <NumberField
              label="Volume (dB)"
              value={gainToDb(clip.gain)}
              rule={{ name: 'Volume', min: VOLUME_DB.min, max: VOLUME_DB.max, unit: 'dB' }}
              disabled={locked}
              oncommit={(v) => void setProps({ gain: dbToGain(v) }, 'Volume')}
            />
            <ClipSlider
              label="Volume"
              value={gainToDb(clip.gain)}
              min={VOLUME_DB.min}
              max={VOLUME_DB.max}
              step={0.5}
              format={db}
              disabled={locked}
              oncommit={(v) => void setProps({ gain: dbToGain(v) }, 'Volume')}
            />
            <Checkbox
              label="Mute"
              checked={clip.muted}
              disabled={locked}
              onchange={(e) => void setProps({ muted: e.currentTarget.checked }, 'Mute')}
            />
          {:else if (id as TabId) === 'speed'}
            <NumberField
              label="Speed (x)"
              value={1}
              rule={{ name: 'Speed', min: 0.25, max: 4, unit: 'x' }}
              disabled
              oncommit={() => {}}
            />
            <p class="hint" data-testid="speed-note">
              Arrives with the speed edit: the engine has no clip speed yet, so this control cannot
              change the clip.
            </p>
          {:else if (id as TabId) === 'crop'}
            <div class="presets" role="group" aria-label="Crop to a shape">
              {#each ['9:16', '1:1', '16:9'] as const as preset (preset)}
                <Button
                  variant={activePreset === preset ? 'primary' : 'secondary'}
                  size="sm"
                  aria-pressed={activePreset === preset}
                  disabled={locked || sourceW === null}
                  onclick={() => applyPreset(preset)}
                >
                  {preset}
                </Button>
              {/each}
              <Button
                variant="ghost"
                size="sm"
                disabled={locked || crop === null}
                onclick={() => {
                  cropError = '';
                  void setProps({ crop: null }, 'Crop');
                }}
              >
                Full frame
              </Button>
            </div>
            {#if sourceW === null}
              <p class="hint">
                The picture size of this clip is not known yet, so the shapes are off.
              </p>
            {/if}
            <p class="hint">
              Region of the source picture, in percent. Dragging a box on the preview arrives with
              the preview.
            </p>
            <div class="row">
              {#each [['x', 'Left'], ['y', 'Top'], ['w', 'Width'], ['h', 'Height']] as const as [field, name] (field)}
                <NumberField
                  label="{name} (%)"
                  value={(crop ?? { x: 0, y: 0, w: 1, h: 1 })[field]}
                  rule={cropRule(name)}
                  toDisplay={(v) => v * 100}
                  fromDisplay={(v) => v / 100}
                  disabled={locked}
                  oncommit={(v) => setCropField(field, v * 100)}
                />
              {/each}
            </div>
            {#if cropError}<p class="error" role="alert">! {cropError}</p>{/if}
          {:else}
            <Select
              label="Style"
              value="default"
              disabled
              options={[{ value: 'default', label: 'Default' }]}
            />
            <Select
              label="Word highlight"
              value="off"
              disabled
              options={[{ value: 'off', label: 'Off' }]}
            />
            <Select label="Position" value="bottom" disabled options={POSITIONS} />
            <NumberField
              label="Size"
              value={0.05}
              rule={{ name: 'Size', min: 1, max: 20, unit: '%' }}
              disabled
              oncommit={() => {}}
            />
            <p class="hint" data-testid="captions-note">
              Arrives with captions: they are not clips yet, so there is nothing to style. They are
              burned in when a short is exported.
            </p>
          {/if}
        </div>
      {/snippet}
    </Tabs>
  {/if}
</div>

<style>
  .clip-inspector {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
  }

  .group {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding-top: var(--space-3);
  }

  .row {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--space-3);
  }

  .presets {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    font-size: var(--font-size-sm);
  }

  .note,
  .hint {
    margin: 0;
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
  }

  .error {
    margin: 0;
    color: var(--color-danger);
    font-size: var(--font-size-xs);
  }
</style>
