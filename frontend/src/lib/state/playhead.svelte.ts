/**
 * The playhead: where the timeline is being looked at, in seconds. The preview reads it for its time readout, the
 * ruler and lanes draw it. Not part of the project and never sent to the server (ADR 0004, "State"). The user moves it
 * with `seek` (which tells `onSeek` listeners); playback moves it with `advance`, which does not, so a listener hears
 * only the user.
 */
export class PlayheadStore {
  time = $state(0);
  /** The length the playhead may move within; the timeline sets it. */
  duration = $state(0);

  // Plain bookkeeping, not UI state: nothing renders from it.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #seeks = new Set<(time: number) => void>();

  seek(seconds: number): void {
    this.advance(seconds);
    for (const listener of this.#seeks) listener(this.time);
  }

  /** Move the playhead without it counting as a seek: what playback does as the picture advances. */
  advance(seconds: number): void {
    const clamped = Math.min(Math.max(0, seconds), this.duration);
    this.time = Math.round(clamped * 1000) / 1000;
  }

  onSeek(listener: (time: number) => void): () => void {
    this.#seeks.add(listener);
    return () => this.#seeks.delete(listener);
  }

  reset(): void {
    this.time = 0;
  }
}

export const playhead = new PlayheadStore();
