import { ThumbLoader } from '../timeline/thumbs';

/**
 * The page's thumbnail loader with a reactive "one more is ready" counter. Every filmstrip says which frames it wants
 * (`want`, keyed by clip); the wanted lists are merged and handed to the loader once per tick, so it holds one queue
 * and a few requests in flight whatever the number of clips on screen.
 */
export class ReactiveThumbs {
  version = $state(0);
  #loader = new ThumbLoader();
  // Plain bookkeeping, nothing renders from it.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #wanted = new Map<string, readonly string[]>();
  #scheduled = false;

  constructor() {
    this.#loader.onChange = () => {
      this.version += 1;
    };
  }

  /** Whether the frame can be drawn (reading this inside a component re-runs it when more frames arrive). */
  isLoaded(url: string): boolean {
    void this.version;
    return this.#loader.isLoaded(url);
  }

  want(key: string, urls: readonly string[]): void {
    this.#wanted.set(key, urls);
    this.#schedule();
  }

  drop(key: string): void {
    this.#wanted.delete(key);
    this.#schedule();
  }

  #schedule(): void {
    if (this.#scheduled) return;
    this.#scheduled = true;
    queueMicrotask(() => {
      this.#scheduled = false;
      this.#loader.request([...this.#wanted.values()].flat());
    });
  }
}

export const thumbs = new ReactiveThumbs();
