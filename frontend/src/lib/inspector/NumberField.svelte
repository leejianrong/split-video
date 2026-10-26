<script lang="ts">
  import { untrack } from 'svelte';
  import TextField from '../ui/TextField.svelte';
  import { format, parseNumber, type NumberRule } from './fields';

  // A numeric inspector field. It edits a local text and commits ONE value on change (Enter or leaving the field), so
  // typing never spams the revision. A bad value stays in the box with a "Fix:" hint and nothing is sent.
  type Props = {
    label: string;
    value: number;
    rule: NumberRule;
    /** The number as shown (percent for a fraction); `fromDisplay` is the inverse. */
    toDisplay?: (value: number) => number;
    fromDisplay?: (shown: number) => number;
    oncommit: (value: number) => void;
    disabled?: boolean;
    hint?: string;
  };

  let {
    label,
    value,
    rule,
    toDisplay = (v) => v,
    fromDisplay = (v) => v,
    oncommit,
    disabled = false,
    hint,
  }: Props = $props();

  let text = $state('');
  let error = $state('');

  // Follow the clip: after an edit, a reload or an agent's change the box shows what the engine holds.
  // Only a changed `value` resets the box (not a re-created `toDisplay`), so typing is not overwritten by a re-render.
  $effect(() => {
    const shown = value;
    untrack(() => {
      text = format(toDisplay(shown));
      error = '';
    });
  });

  function commit() {
    const parsed = parseNumber(text, rule);
    if (!parsed.ok) {
      error = parsed.error;
      return;
    }
    error = '';
    if (Math.abs(parsed.value - toDisplay(value)) < 1e-9) return;
    oncommit(fromDisplay(parsed.value));
  }
</script>

<TextField
  {label}
  bind:value={text}
  {error}
  {hint}
  {disabled}
  inputmode="decimal"
  autocomplete="off"
  onchange={commit}
/>
