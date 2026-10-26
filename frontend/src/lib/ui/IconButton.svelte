<script lang="ts">
  import type { HTMLButtonAttributes } from 'svelte/elements';
  import Button, { type ButtonSize, type ButtonVariant } from './Button.svelte';
  import Icon, { type IconName } from './icons/Icon.svelte';
  import Tooltip from './Tooltip.svelte';

  // An icon-only button: `label` is required and becomes the accessible name and the tooltip. `action` is a
  // shortcut id; its keys show in the tooltip.
  type Props = Omit<HTMLButtonAttributes, 'children' | 'aria-label'> & {
    label: string;
    icon: IconName;
    action?: string;
    variant?: ButtonVariant;
    size?: ButtonSize;
  };

  let { label, icon, action, variant = 'ghost', size = 'md', ...rest }: Props = $props();
</script>

<Tooltip {label} {action}>
  <Button iconOnly {variant} {size} aria-label={label} {...rest}>
    <Icon name={icon} size={size === 'lg' ? 22 : 18} />
  </Button>
</Tooltip>
