<script lang="ts">
  import Icon from '../ui/icons/Icon.svelte';
  import type { Lane } from './lanes';

  // The header of a lane: its name and the toggles of ux-spec #timeline. Lock on every lane, hide on video lanes, mute
  // on audio lanes. A toggle changes the track's flag through the engine (`set_track_flags`), so what the header shows
  // is always what is saved. State is shown by an icon and the pressed state, never by colour alone.
  let { lane, ontoggle }: { lane: Lane; ontoggle: (flag: 'locked' | 'hidden' | 'muted') => void } =
    $props();

  const track = $derived(lane.track);
</script>

<div
  class="head"
  class:locked={track.locked}
  style:height="{lane.height}px"
  data-testid="lane-header"
  data-lane-id={lane.id}
  data-kind={lane.kind}
>
  <span class="name">{lane.name}</span>
  <span class="toggles">
    <button
      type="button"
      aria-pressed={track.locked}
      aria-label="Lock {lane.name}"
      title={track.locked ? `Unlock ${lane.name}` : `Lock ${lane.name}`}
      data-testid="toggle-lock"
      onclick={() => ontoggle('locked')}
    >
      <Icon name={track.locked ? 'lock' : 'unlock'} size={14} />
    </button>
    {#if lane.canHide}
      <button
        type="button"
        aria-pressed={track.hidden}
        aria-label="Hide {lane.name}"
        title={track.hidden ? `Show ${lane.name}` : `Hide ${lane.name}`}
        data-testid="toggle-hide"
        onclick={() => ontoggle('hidden')}
      >
        <Icon name={track.hidden ? 'eyeOff' : 'eye'} size={14} />
      </button>
    {/if}
    {#if lane.canMute}
      <button
        type="button"
        aria-pressed={track.muted}
        aria-label="Mute {lane.name}"
        title={track.muted ? `Unmute ${lane.name}` : `Mute ${lane.name}`}
        data-testid="toggle-mute"
        onclick={() => ontoggle('muted')}
      >
        <Icon name={track.muted ? 'volumeOff' : 'volume'} size={14} />
      </button>
    {/if}
  </span>
</div>

<style>
  .head {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 2px;
    box-sizing: border-box;
    padding: 0 var(--space-2) 0 var(--space-3);
    border-bottom: 1px solid var(--color-border);
    color: var(--color-text-muted);
    font-size: var(--font-size-xs);
  }

  .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .toggles {
    display: flex;
    gap: var(--space-1);
  }

  button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 20px;
    padding: 0;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--color-text-muted);
    cursor: pointer;
  }

  button:hover {
    background: var(--color-surface-hover);
    color: var(--color-text);
  }

  button[aria-pressed='true'] {
    border-color: var(--color-border-strong);
    background: var(--color-surface-active);
    color: var(--color-text);
  }

  button:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 1px;
  }
</style>
