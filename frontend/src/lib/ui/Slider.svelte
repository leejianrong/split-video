<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';

  // A labelled native range input with a value readout. Keyboard: arrows step, Home/End jump, PageUp/PageDown (a larger step)
  // take a larger step (all native). `format` shapes the readout and the spoken value (aria-valuetext).
  type Props = Omit<HTMLInputAttributes, 'value' | 'type' | 'children'> & {
    label: string;
    value: number;
    min?: number;
    max?: number;
    step?: number;
    format?: (value: number) => string;
    /** A track only, for a toolbar: the label stays for assistive technology and the readout is dropped. */
    inline?: boolean;
  };

  let {
    label,
    value = $bindable(),
    min = 0,
    max = 100,
    step = 1,
    format = (v: number) => String(v),
    inline = false,
    disabled = false,
    id: idProp,
    ...rest
  }: Props = $props();

  const uid = $props.id();
  const id = $derived(idProp ?? `slider-${uid}`);
  const fill = $derived(max > min ? ((value - min) / (max - min)) * 100 : 0);
  const text = $derived(format(value));
</script>

<div class="slider" class:disabled class:inline>
  <label for={id} class:sr-only={inline}>{label}</label>
  <input
    {id}
    type="range"
    {min}
    {max}
    {step}
    {disabled}
    bind:value
    aria-valuetext={text}
    style:--fill="{fill}%"
    {...rest}
  />
  <output for={id} class:sr-only={inline}>{text}</output>
</div>

<style>
  .slider {
    display: grid;
    grid-template-columns: 1fr auto;
    grid-template-areas:
      'label out'
      'track track';
    align-items: baseline;
    gap: var(--space-1) var(--space-3);
  }

  .inline {
    grid-template-columns: 1fr;
    grid-template-areas: 'track';
  }

  label {
    grid-area: label;
    color: var(--color-text);
    font-size: var(--font-size-sm);
  }

  output {
    grid-area: out;
    color: var(--color-text-muted);
    font-size: var(--font-size-sm);
    font-variant-numeric: tabular-nums;
  }

  input {
    grid-area: track;
    width: 100%;
    height: var(--control-height-sm);
    margin: 0;
    background: transparent;
    appearance: none;
    cursor: pointer;
  }

  input::-webkit-slider-runnable-track {
    height: 4px;
    border-radius: var(--radius-full);
    background: linear-gradient(
      to right,
      var(--color-accent) var(--fill),
      var(--color-border-strong) var(--fill)
    );
  }
  input::-moz-range-track {
    height: 4px;
    border-radius: var(--radius-full);
    background: var(--color-border-strong);
  }
  input::-moz-range-progress {
    height: 4px;
    border-radius: var(--radius-full);
    background: var(--color-accent);
  }

  input::-webkit-slider-thumb {
    width: 16px;
    height: 16px;
    margin-top: -6px;
    border: 2px solid var(--color-bg);
    border-radius: 50%;
    background: var(--color-text);
    appearance: none;
  }
  input::-moz-range-thumb {
    width: 12px;
    height: 12px;
    border: 2px solid var(--color-bg);
    border-radius: 50%;
    background: var(--color-text);
  }
  input:hover:not(:disabled)::-webkit-slider-thumb {
    background: var(--color-accent-hover);
  }
  input:hover:not(:disabled)::-moz-range-thumb {
    background: var(--color-accent-hover);
  }

  input:disabled {
    cursor: not-allowed;
  }
  .disabled label,
  .disabled output {
    color: var(--color-text-disabled);
  }
  input:disabled::-webkit-slider-runnable-track {
    background: var(--color-border);
  }
  input:disabled::-webkit-slider-thumb {
    background: var(--color-text-disabled);
  }
  input:disabled::-moz-range-track,
  input:disabled::-moz-range-progress {
    background: var(--color-border);
  }
  input:disabled::-moz-range-thumb {
    background: var(--color-text-disabled);
  }
</style>
