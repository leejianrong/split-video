/** What one still is: the project, the instant, the pixel width asked for, and the revision it must show. */
export interface StillKey {
  path: string;
  time: number;
  width: number;
  /** The export preset the canvas stands for: it decides the frame's shape. */
  preset: string;
  revision: number | null;
}

export interface StillHandlers<T> {
  onStill(still: T, key: StillKey): void;
  onError(cause: unknown, key: StillKey): void;
  /** True from the moment a still is wanted until it arrived, failed or was replaced by the next one. */
  onBusy(busy: boolean): void;
}

/** Wait this long after the playhead stops before asking for a still: short enough that a still lands inside the ux-spec's 500 ms (a frame renders in well under 300 ms), long enough that a drag, which moves every 16 ms, sends one request instead of dozens. */
export const STILL_DELAY_MS = 150;

const same = (a: StillKey | null, b: StillKey) =>
  a !== null &&
  a.path === b.path &&
  a.time === b.time &&
  a.width === b.width &&
  a.preset === b.preset &&
  a.revision === b.revision;

/**
 * Decides when a still is requested: debounced, one at a time, newest wins. A new key cancels the pending timer and
 * aborts the request in flight (its answer would be stale), and an answer that is not the newest is dropped even if
 * `fetchStill` ignores the abort signal. Asking for the key already shown or on its way does nothing.
 */
export class StillScheduler<T> {
  #latest: StillKey | null = null;
  #timer: ReturnType<typeof setTimeout> | null = null;
  #controller: AbortController | null = null;

  constructor(
    private readonly fetchStill: (key: StillKey, signal: AbortSignal) => Promise<T>,
    private readonly handlers: StillHandlers<T>,
    private readonly delayMs = STILL_DELAY_MS,
  ) {}

  request(key: StillKey): void {
    if (same(this.#latest, key)) return;
    this.#stop();
    this.#latest = key;
    this.handlers.onBusy(true);
    this.#timer = setTimeout(() => void this.#run(key), this.delayMs);
  }

  /** Forget everything: nothing pending, nothing in flight, the next request always goes out. */
  cancel(): void {
    this.#stop();
    this.#latest = null;
    this.handlers.onBusy(false);
  }

  #stop(): void {
    if (this.#timer !== null) clearTimeout(this.#timer);
    this.#timer = null;
    this.#controller?.abort();
    this.#controller = null;
  }

  async #run(key: StillKey): Promise<void> {
    this.#timer = null;
    const controller = new AbortController();
    this.#controller = controller;
    try {
      const still = await this.fetchStill(key, controller.signal);
      if (this.#controller !== controller || controller.signal.aborted) return;
      this.#controller = null;
      this.handlers.onBusy(false);
      this.handlers.onStill(still, key);
    } catch (cause) {
      if (this.#controller !== controller || controller.signal.aborted) return;
      this.#controller = null;
      this.#latest = null; // so the next request retries instead of being taken for the failed one
      this.handlers.onBusy(false);
      this.handlers.onError(cause, key);
    }
  }
}
