import { getVideoPreviewFrameApiV1ProjectPreviewFrameGet } from '../api/client';
import { toProjectError, type ProjectError } from './project.svelte';
import { StillScheduler, type StillKey } from './stills';

interface Still {
  blob: Blob;
  /** The timeline second the picture is of (the output's frame grid, at most one frame before the playhead). */
  time: number;
  /** Width over height of the output, from the server (the project's ratio). */
  ratio: number;
}

async function fetchStill(key: StillKey, signal: AbortSignal): Promise<Still> {
  const { data, response } = await getVideoPreviewFrameApiV1ProjectPreviewFrameGet({
    query: { path: key.path, time: key.time, width: key.width },
    parseAs: 'blob',
    signal,
  });
  const w = Number(response.headers.get('X-Preview-Width'));
  const h = Number(response.headers.get('X-Preview-Height'));
  return {
    blob: data as unknown as Blob,
    time: Number(response.headers.get('X-Preview-Time') ?? key.time),
    ratio: w > 0 && h > 0 ? w / h : 16 / 9,
  };
}

/**
 * The picture in the preview: the composite of the timeline at the playhead, rendered by the server with the export's
 * own filter graph (ADR 0002) and kept as an object URL. While the next still renders `busy` is true and the previous
 * `url` stays; a failure leaves it too and sets `error` (calm, inline: editing carries on).
 */
export class PreviewStore {
  url = $state<string | null>(null);
  time = $state<number | null>(null);
  ratio = $state(16 / 9);
  busy = $state(false);
  error = $state<ProjectError | null>(null);

  #path: string | null = null;
  #scheduler: StillScheduler<Still>;

  constructor(fetcher: (key: StillKey, signal: AbortSignal) => Promise<Still> = fetchStill) {
    this.#scheduler = new StillScheduler(fetcher, {
      onStill: (still) => {
        this.#setUrl(URL.createObjectURL(still.blob));
        this.time = still.time;
        this.ratio = still.ratio;
        this.error = null;
      },
      onError: (cause) => {
        this.error = toProjectError(cause);
      },
      onBusy: (busy) => (this.busy = busy),
    });
  }

  /** Ask for the still of `key` (debounced, see `StillScheduler`). A different project drops the old picture first. */
  request(key: StillKey): void {
    if (this.#path !== key.path) {
      this.#path = key.path;
      this.clear();
    }
    this.#scheduler.request(key);
  }

  /** Nothing to show (an empty timeline, or the editor was left): cancel what is pending and drop the picture. */
  clear(): void {
    this.#scheduler.cancel();
    this.#setUrl(null);
    this.time = null;
    this.error = null;
  }

  #setUrl(next: string | null): void {
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = next;
  }
}

export const preview = new PreviewStore();
