import {
  ApiError,
  getWaveformApiV1AssetsAssetIdWaveformGet,
  startWaveformApiV1AssetsAssetIdWaveformPost,
  type AssetWaveformOut,
} from '../api/client';

export type WaveformStatus = 'loading' | 'computing' | 'ready' | 'none' | 'failed';

export interface WaveformEntry {
  status: WaveformStatus;
  data: AssetWaveformOut | null;
}

/** Poll for the computed curves every 2 s, for at most 20 minutes (a three-hour recording takes about one). */
const POLL_MS = 2000;
const POLL_LIMIT_MS = 20 * 60 * 1000;

/**
 * The stored waveforms of the assets on the timeline, by asset id. The server decodes the audio once, as a job, and
 * keeps the curves next to the recording; this only asks for them: a 404 "no waveform yet" starts the job and the
 * lane draws a placeholder until the curves are there. An asset without audio is `none`.
 */
export class WaveformStore {
  entries = $state<Record<string, WaveformEntry>>({});
  // Plain bookkeeping, nothing renders from it.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #timers = new Map<string, ReturnType<typeof setTimeout>>();
  #disposed = false;

  get(assetId: string): WaveformEntry | undefined {
    return this.entries[assetId];
  }

  /** Make sure `assetId` is being fetched or computed; calling it again is free. */
  ensure(assetId: string): void {
    if (this.#disposed || !assetId || this.entries[assetId]) return;
    this.#set(assetId, 'loading');
    void this.#run(assetId);
  }

  #set(id: string, status: WaveformStatus, data: AssetWaveformOut | null = null): void {
    this.entries = { ...this.entries, [id]: { status, data } };
  }

  async #fetch(id: string): Promise<AssetWaveformOut | null> {
    try {
      const { data } = await getWaveformApiV1AssetsAssetIdWaveformGet({ path: { asset_id: id } });
      return data;
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) {
        if (/no audio/i.test(cause.message)) throw new NoAudio();
        return null;
      }
      throw cause;
    }
  }

  async #run(id: string): Promise<void> {
    try {
      const found = await this.#fetch(id);
      if (found) return this.#set(id, 'ready', found);
      this.#set(id, 'computing');
      await startWaveformApiV1AssetsAssetIdWaveformPost({ path: { asset_id: id } });
      const deadline = Date.now() + POLL_LIMIT_MS;
      const poll = async (): Promise<void> => {
        if (this.#disposed) return;
        try {
          const data = await this.#fetch(id);
          if (data) return this.#set(id, 'ready', data);
        } catch (cause) {
          if (cause instanceof NoAudio) return this.#set(id, 'none');
        }
        if (Date.now() > deadline) return this.#set(id, 'failed');
        this.#timers.set(
          id,
          setTimeout(() => void poll(), POLL_MS),
        );
      };
      this.#timers.set(
        id,
        setTimeout(() => void poll(), POLL_MS / 4),
      );
    } catch (cause) {
      const noAudio =
        cause instanceof NoAudio || (cause instanceof ApiError && cause.status === 422);
      this.#set(id, noAudio ? 'none' : 'failed');
    }
  }

  /** Stop polling and forget everything (the project closed). */
  reset(): void {
    for (const timer of this.#timers.values()) clearTimeout(timer);
    this.#timers.clear();
    this.entries = {};
  }

  dispose(): void {
    this.reset();
    this.#disposed = true;
  }
}

class NoAudio extends Error {}

export const waveforms = new WaveformStore();
