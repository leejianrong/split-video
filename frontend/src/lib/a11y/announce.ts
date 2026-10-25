/**
 * A polite live region for messages that are not tied to a visible element: job completion, agent edits, errors
 * (ux-spec #accessibility). The element is created on first use and lives for the page.
 */
const REGION_ID = 'sv-live-region';

function region(): HTMLElement {
  let el = document.getElementById(REGION_ID);
  if (!el) {
    el = document.createElement('div');
    el.id = REGION_ID;
    el.className = 'sr-only';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.setAttribute('aria-atomic', 'true');
    document.body.append(el);
  }
  return el;
}

const NBSP = String.fromCharCode(160);
let flip = false;

/** Say `message` to assistive technology without moving focus. Repeating a message is announced again. */
export function announce(message: string): void {
  const el = region();
  el.textContent = '';
  // A trailing no-break space makes an identical message a change, so it is spoken again.
  flip = !flip;
  el.textContent = flip ? message : message + NBSP;
}
