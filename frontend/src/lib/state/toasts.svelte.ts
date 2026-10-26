export type ToastKind = 'info' | 'success' | 'warning' | 'error';

export interface ToastAction {
  label: string;
  onclick: () => void;
}

export interface ToastOptions {
  kind?: ToastKind;
  message: string;
  action?: ToastAction;
  /** Milliseconds before it goes away. `null` keeps it until dismissed; errors default to that. */
  duration?: number | null;
}

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
  action?: ToastAction;
  duration: number | null;
}

export const DEFAULT_DURATION = 6000;

interface Timer {
  handle: ReturnType<typeof setTimeout> | undefined;
  startedAt: number;
  remaining: number;
}

/**
 * The toast queue. Info, success and warning dismiss themselves after `duration`; the timer pauses while the
 * pointer or focus is on the toast (`pause`/`resume`). Errors stay until dismissed (ux-spec principle 6: they say
 * what to try, so they must not vanish before they are read).
 */
export class ToastStore {
  items = $state<Toast[]>([]);
  #nextId = 1;
  // Plain timers, not UI state: nothing renders from this map, so it does not need to be reactive.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #timers = new Map<number, Timer>();

  show(options: ToastOptions): number {
    const kind = options.kind ?? 'info';
    const duration =
      options.duration !== undefined
        ? options.duration
        : kind === 'error'
          ? null
          : DEFAULT_DURATION;
    const toast: Toast = {
      id: this.#nextId++,
      kind,
      message: options.message,
      action: options.action,
      duration,
    };
    this.items.push(toast);
    if (duration !== null) this.#start(toast.id, duration);
    return toast.id;
  }

  error(message: string, action?: ToastAction): number {
    return this.show({ kind: 'error', message, action });
  }

  dismiss(id: number): void {
    const timer = this.#timers.get(id);
    if (timer) clearTimeout(timer.handle);
    this.#timers.delete(id);
    this.items = this.items.filter((t) => t.id !== id);
  }

  clear(): void {
    for (const id of [...this.#timers.keys()]) this.dismiss(id);
    this.items = [];
  }

  pause(id: number): void {
    const timer = this.#timers.get(id);
    if (!timer || timer.handle === undefined) return;
    clearTimeout(timer.handle);
    timer.handle = undefined;
    timer.remaining = Math.max(0, timer.remaining - (Date.now() - timer.startedAt));
  }

  resume(id: number): void {
    const timer = this.#timers.get(id);
    if (!timer || timer.handle !== undefined) return;
    this.#start(id, timer.remaining);
  }

  #start(id: number, ms: number): void {
    this.#timers.set(id, {
      handle: setTimeout(() => this.dismiss(id), ms),
      startedAt: Date.now(),
      remaining: ms,
    });
  }
}

export const toasts = new ToastStore();
