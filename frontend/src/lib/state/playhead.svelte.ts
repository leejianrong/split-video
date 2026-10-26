/**
 * The playhead: where the timeline is being looked at, in seconds. The preview reads it for its time readout, the
 * ruler and lanes draw it. Not part of the project and never sent to the server (ADR 0004, "State"); the preview does
 * not play yet, so only the user moves it.
 */
export class PlayheadStore {
  time = $state(0);
  /** The length the playhead may move within; the timeline sets it. */
  duration = $state(0);

  seek(seconds: number): void {
    const clamped = Math.min(Math.max(0, seconds), this.duration);
    this.time = Math.round(clamped * 1000) / 1000;
  }

  reset(): void {
    this.time = 0;
  }
}

export const playhead = new PlayheadStore();
