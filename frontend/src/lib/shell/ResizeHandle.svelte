<script lang="ts">
  // A drag handle on a region's edge, as an ARIA window splitter: focusable, `role="separator"` with
  // aria-valuenow/min/max (the size of the region it resizes), arrow keys (Shift = bigger steps), Home and End
  // for the limits, double click to reset. It sits on the edge and takes no layout space.
  type Props = {
    /** The region's name, for the accessible label: "Resize the left panel". */
    label: string;
    /** `vertical` is a vertical line (resizes a width); `horizontal` resizes a height. */
    orientation: 'vertical' | 'horizontal';
    value: number;
    min: number;
    max: number;
    /**
     * +1 when moving the pointer right (or down) makes the region bigger, -1 when it makes it smaller: the left
     * panel grows to the right, the inspector and the timeline grow to the left and up.
     */
    direction: 1 | -1;
    /** Which side of its region the handle sits on. */
    edge: 'left' | 'right' | 'top';
    onchange: (value: number) => void;
    onreset: () => void;
  };

  let { label, orientation, value, min, max, direction, edge, onchange, onreset }: Props = $props();

  const STEP = 10;
  const BIG_STEP = 50;
  let dragging = $state(false);
  let startPointer = 0;
  let startValue = 0;

  const axis = (event: PointerEvent) =>
    orientation === 'vertical' ? event.clientX : event.clientY;

  function onpointerdown(event: PointerEvent) {
    if (event.button !== 0) return;
    dragging = true;
    startPointer = axis(event);
    startValue = value;
    // Pointer capture keeps the drag going outside the handle; not every environment supports it.
    try {
      (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    } catch {
      /* the drag still works while the pointer is over the handle */
    }
    event.preventDefault();
  }

  function onpointermove(event: PointerEvent) {
    if (!dragging) return;
    onchange(startValue + direction * (axis(event) - startPointer));
  }

  function stop() {
    dragging = false;
  }

  function onkeydown(event: KeyboardEvent) {
    const grow = orientation === 'vertical' ? 'ArrowRight' : 'ArrowDown';
    const shrink = orientation === 'vertical' ? 'ArrowLeft' : 'ArrowUp';
    const step = event.shiftKey ? BIG_STEP : STEP;
    let next: number | null = null;
    if (event.key === grow) next = value + direction * step;
    else if (event.key === shrink) next = value - direction * step;
    else if (event.key === 'Home') next = min;
    else if (event.key === 'End') next = max;
    else if (event.key === 'Enter') {
      onreset();
      event.preventDefault();
      return;
    }
    if (next === null) return;
    // Handled here, so the global shortcuts (Home, End, arrows) leave the key alone.
    event.preventDefault();
    onchange(next);
  }
</script>

<!-- A separator that can be focused is an interactive widget (WAI-ARIA window splitter), so the a11y rule that
     treats separators as static does not apply. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  class="handle {orientation} {edge}"
  class:dragging
  role="separator"
  aria-orientation={orientation}
  aria-label={label}
  aria-valuenow={Math.round(value)}
  aria-valuemin={min}
  aria-valuemax={max}
  tabindex="0"
  {onpointerdown}
  {onpointermove}
  onpointerup={stop}
  onpointercancel={stop}
  onlostpointercapture={stop}
  ondblclick={onreset}
  {onkeydown}
>
  <span class="line"></span>
</div>

<style>
  .handle {
    position: absolute;
    z-index: var(--z-sticky);
    display: flex;
    align-items: center;
    justify-content: center;
    touch-action: none;
  }
  /* A 7 px hit area centred on the region edge. */
  .vertical {
    top: 0;
    bottom: 0;
    width: 7px;
    cursor: col-resize;
  }
  .vertical.right {
    right: -4px;
  }
  .vertical.left {
    left: -4px;
  }
  .horizontal {
    left: 0;
    right: 0;
    height: 7px;
    cursor: row-resize;
  }
  .horizontal.top {
    top: -4px;
  }

  .line {
    background: transparent;
    transition: background var(--duration-fast);
  }
  .vertical .line {
    width: 2px;
    height: 100%;
  }
  .horizontal .line {
    height: 2px;
    width: 100%;
  }
  .handle:hover .line,
  .handle.dragging .line {
    background: var(--color-accent);
  }
  /* The global focus ring is an outline around the 7 px box, which is too thin to see; light the line instead. */
  .handle:focus-visible {
    outline: none;
  }
  .handle:focus-visible .line {
    background: var(--color-focus);
  }
</style>
