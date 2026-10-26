import {
  ApiError,
  client,
  getAssetApiV1AssetsAssetIdGet,
  listAssetsApiV1AssetsGet,
  makeProxyApiV1AssetsAssetIdProxyPost,
  registerAssetApiV1AssetsRegisterPost,
  uploadFile,
  type AssetOut,
  type UploadOptions,
} from '../api/client';
import { announce } from '../a11y/announce';
import { jobs as defaultJobs, type JobsStore } from './jobs.svelte';
import { toProjectError, type ProjectError } from './project.svelte';

/** The library: every asset in the workspace's asset table, plus the imports in flight (docs/library.md). */
export type LoadStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface Upload {
  id: number;
  /** The file's name as chosen; the card shows it until the server names the asset. */
  name: string;
  size: number;
  loaded: number;
  status: 'queued' | 'uploading' | 'failed';
  error: ProjectError | null;
  file: File;
}

/** What a card shows (ux-spec #left-tabs, Media states). "Preparing preview" is a flag on a ready card. */
export type CardKind = 'ready' | 'importing' | 'failed' | 'missing';

type Uploader = (file: File, options: UploadOptions) => Promise<AssetOut>;

/** The still used as a thumbnail: one frame a little way in (a frame at 0 is often black), never past the end. */
export function posterTime(duration: number): number {
  return Math.max(0, Math.min(duration * 0.1, 30, Math.max(duration - 0.1, 0)));
}

/**
 * The URL of the asset's poster frame: `GET /assets/{id}/frame`, one fast ffmpeg seek on the proxy or the original
 * (never a whole decode), 240 px wide, cached by the browser. `null` for an audio-only or missing asset.
 */
export function posterUrl(asset: AssetOut, width = 240): string | null {
  if (!asset.has_video || !asset.present) return null;
  return client.buildUrl({
    url: '/api/v1/assets/{asset_id}/frame',
    path: { asset_id: asset.id },
    query: { time: Number(posterTime(asset.duration).toFixed(3)), width, format: 'jpeg' },
  });
}

export class AssetsStore {
  items = $state<AssetOut[]>([]);
  status = $state<LoadStatus>('idle');
  error = $state<ProjectError | null>(null);
  uploads = $state<Upload[]>([]);

  #nextUpload = 1;
  #uploader: Uploader;
  #jobs: JobsStore;
  #current: { id: number; abort: AbortController } | null = null;
  #pumping = false;

  constructor(jobs: JobsStore = defaultJobs, uploader: Uploader = uploadFile) {
    this.#jobs = jobs;
    this.#uploader = uploader;
    // A proxy job that ends changes what the card can say (the strip and the proxy exist now).
    jobs.onFinished((job) => {
      if (job.kind !== 'proxy' || job.state !== 'done') return;
      const asset = this.items.find((a) => a.path === job.label);
      if (asset) void this.reload(asset.id);
    });
  }

  byId(id: string | null): AssetOut | undefined {
    return id === null ? undefined : this.items.find((a) => a.id === id);
  }

  byPath(path: string | null): AssetOut | undefined {
    return path === null ? undefined : this.items.find((a) => a.path === path);
  }

  /** The state a card is in: the server's `present`, and the preview job when one is running for it. */
  cardOf(asset: AssetOut): { kind: CardKind; preparing: number | null } {
    if (!asset.present) return { kind: 'missing', preparing: null };
    const job = this.#jobs.latest('proxy', asset.path);
    const preparing =
      job && (job.state === 'queued' || job.state === 'running')
        ? Math.round(Math.min(Math.max(job.fraction, 0), 1) * 100)
        : null;
    return { kind: 'ready', preparing };
  }

  async load(): Promise<void> {
    this.status = 'loading';
    this.error = null;
    try {
      const { data } = await listAssetsApiV1AssetsGet();
      this.items = data.assets;
      this.status = 'ready';
    } catch (cause) {
      this.error = toProjectError(cause);
      this.status = 'error';
    }
  }

  upsert(asset: AssetOut): void {
    const at = this.items.findIndex((a) => a.id === asset.id);
    if (at >= 0) this.items[at] = asset;
    else this.items.push(asset);
  }

  async reload(id: string): Promise<AssetOut | null> {
    try {
      const { data } = await getAssetApiV1AssetsAssetIdGet({ path: { asset_id: id } });
      this.upsert(data);
      return data;
    } catch {
      return null;
    }
  }

  /**
   * Add a file that is already in the workspace (picked from the recordings folder) to the library. Nothing is
   * copied. A video also gets its preview prepared (a `proxy` job, shown as a percent on the card).
   */
  async register(path: string, options: { prepare?: boolean } = {}): Promise<AssetOut> {
    const { data } = await registerAssetApiV1AssetsRegisterPost({ body: { path } });
    this.upsert(data);
    if (options.prepare !== false) void this.prepare(data);
    return data;
  }

  /** Queue the proxy and thumbnail strip for a video that has none. Failure is not fatal: the card stays usable. */
  async prepare(asset: AssetOut): Promise<void> {
    if (!asset.has_video || asset.proxy || !asset.present) return;
    const running = this.#jobs.latest('proxy', asset.path);
    if (running && (running.state === 'queued' || running.state === 'running')) return;
    try {
      const { data } = await makeProxyApiV1AssetsAssetIdProxyPost({
        path: { asset_id: asset.id },
        body: { force: false },
      });
      this.#jobs.watch(data.job_id);
      await this.#jobs.refresh(data.job_id);
    } catch {
      // The preview can be prepared later; importing already worked.
    }
  }

  /** Make sure the open recording is in the library (it is the main clip), without preparing anything. */
  async ensure(path: string): Promise<void> {
    if (this.byPath(path)) return;
    try {
      await this.register(path, { prepare: false });
    } catch {
      // Not fatal: the Media tab simply shows the other assets.
    }
  }

  /** Look for a missing file again (the server finds a renamed or moved copy by its content). */
  async locate(id: string): Promise<boolean> {
    const asset = await this.reload(id);
    return asset?.present === true;
  }

  // --- uploads ------------------------------------------------------------------------------------------

  /** Import files from the computer (a drop or the file picker): uploaded one after the other. */
  enqueue(files: Iterable<File>): void {
    for (const file of files) {
      this.uploads.push({
        id: this.#nextUpload++,
        name: file.name,
        size: file.size,
        loaded: 0,
        status: 'queued',
        error: null,
        file,
      });
    }
    void this.#pump();
  }

  retry(id: number): void {
    const upload = this.uploads.find((u) => u.id === id);
    if (!upload || upload.status !== 'failed') return;
    upload.status = 'queued';
    upload.error = null;
    upload.loaded = 0;
    void this.#pump();
  }

  /** Stop an upload in flight (the server removes the partial file) or drop a failed or waiting one. */
  cancelUpload(id: number): void {
    if (this.#current?.id === id) this.#current.abort.abort();
    else this.uploads = this.uploads.filter((u) => u.id !== id);
  }

  async #pump(): Promise<void> {
    if (this.#pumping) return;
    this.#pumping = true;
    try {
      for (;;) {
        const next = this.uploads.find((u) => u.status === 'queued');
        if (!next) break;
        await this.#upload(next.id);
      }
    } finally {
      this.#pumping = false;
    }
  }

  async #upload(id: number): Promise<void> {
    const upload = this.uploads.find((u) => u.id === id);
    if (!upload) return;
    upload.status = 'uploading';
    const abort = new AbortController();
    this.#current = { id, abort };
    try {
      const asset = await this.#uploader(upload.file, {
        signal: abort.signal,
        onProgress: (loaded) => {
          upload.loaded = loaded;
        },
      });
      this.uploads = this.uploads.filter((u) => u.id !== id);
      this.upsert(asset);
      announce(`Imported ${asset.path}`);
      void this.prepare(asset);
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === 'cancelled') {
        this.uploads = this.uploads.filter((u) => u.id !== id);
      } else {
        upload.status = 'failed';
        upload.error = toProjectError(cause);
        announce(`Could not import ${upload.name}. ${upload.error.message} ${upload.error.fix}`);
      }
    } finally {
      this.#current = null;
    }
  }
}

export const assets = new AssetsStore();
