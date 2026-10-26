<script lang="ts">
  // A progress bar that exposes its value (ux-spec #accessibility). `value` is 0 to 1, or null while the amount of
  // work is not known (then it is an indeterminate bar and carries no aria-valuenow).
  let { value, label }: { value: number | null; label: string } = $props();
  const percent = $derived(
    value === null ? null : Math.round(Math.min(Math.max(value, 0), 1) * 100),
  );
</script>

<div
  class="bar"
  class:indeterminate={percent === null}
  role="progressbar"
  aria-label={label}
  aria-valuemin={0}
  aria-valuemax={100}
  aria-valuenow={percent ?? undefined}
  aria-valuetext={percent === null ? 'Working' : `${percent}%`}
>
  <span class="fill" style:width={percent === null ? undefined : `${percent}%`}></span>
</div>

<style>
  .bar {
    position: relative;
    height: 4px;
    overflow: hidden;
    border-radius: var(--radius-full);
    background: var(--color-surface-active);
  }

  .fill {
    display: block;
    height: 100%;
    border-radius: inherit;
    background: var(--color-accent);
    transition: width var(--duration-base);
  }

  .indeterminate .fill {
    position: absolute;
    width: 40%;
    animation: slide 1.4s ease-in-out infinite;
  }

  @keyframes slide {
    from {
      left: -40%;
    }
    to {
      left: 100%;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .indeterminate .fill {
      width: 100%;
      animation: none;
      opacity: 0.5;
    }
    .fill {
      transition: none;
    }
  }
</style>
