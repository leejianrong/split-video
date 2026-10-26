<script lang="ts">
  import Slider from '../ui/Slider.svelte';

  // A slider that previews while dragging but commits once, on release or after a key step (the native `change`).
  type Props = {
    label: string;
    value: number;
    min: number;
    max: number;
    step: number;
    format?: (value: number) => string;
    oncommit: (value: number) => void;
    disabled?: boolean;
  };

  let { label, value, min, max, step, format, oncommit, disabled = false }: Props = $props();

  let draft = $derived(value);

  // `change` fires on mouse release, but once per key step with the keyboard, so a held arrow would write a revision
  // per step: wait for the value to settle.
  let timer: ReturnType<typeof setTimeout> | undefined;
  function settle() {
    clearTimeout(timer);
    const wanted = draft;
    timer = setTimeout(() => {
      if (wanted !== value) oncommit(wanted);
    }, 350);
  }
  $effect(() => () => clearTimeout(timer));
</script>

<Slider {label} bind:value={draft} {min} {max} {step} {format} {disabled} onchange={settle} />
