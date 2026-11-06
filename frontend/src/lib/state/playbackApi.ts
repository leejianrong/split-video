import {
  cancelRequestApiV1PreviewRangeRequestsKeyDelete,
  getVideoChunkApiV1ProjectPreviewRangeKeyGet,
  startVideoApiV1ProjectPreviewRangePost,
} from '../api/client';
import { isFinished, jobs } from './jobs.svelte';

export interface Started {
  jobId: string;
  /** The chunk was already rendered for this revision: nothing to wait for. */
  cached: boolean;
  key: string;
}

/**
 * What playback asks of the server: the preview-range endpoints (SPL-70). A request carries a client-made key, so the
 * render can be cancelled by it even when the request is aborted before the job id comes back (SPL-128).
 */
export interface ChunkApi {
  start(
    path: string,
    range: { start: number; end: number; preset: string },
    requestKey: string,
    signal: AbortSignal,
  ): Promise<Started>;
  /** Resolves when the render is done; rejects when it failed or was cancelled. */
  finished(jobId: string, signal: AbortSignal): Promise<void>;
  cancel(requestKey: string): Promise<unknown>;
  fetch(path: string, key: string, signal: AbortSignal): Promise<Blob>;
}

/** A render that has not finished after this long is given up on (a chunk takes seconds). */
const RENDER_TIMEOUT_MS = 90_000;

export const playbackApi: ChunkApi = {
  async start(path, range, requestKey, signal) {
    const { data } = await startVideoApiV1ProjectPreviewRangePost({
      query: { path },
      body: range,
      headers: { 'Idempotency-Key': requestKey },
      signal,
    });
    return { jobId: data.job_id, cached: data.cached, key: data.key };
  },

  finished(jobId, signal) {
    return new Promise<void>((resolve, reject) => {
      const done = (error?: Error) => {
        clearTimeout(timer);
        off();
        signal.removeEventListener('abort', onAbort);
        if (error) reject(error);
        else resolve();
      };
      const settle = (job: { state: string }) => {
        if (job.state === 'done') done();
        else if (isFinished(job as never)) done(new Error(`preview render ${job.state}`));
      };
      const onAbort = () => done(new Error('aborted'));
      // The event stream is the only source (the browser never polls); `watch` makes the finish event reach us even if
      // the job was first seen already running.
      const off = jobs.onFinished((job) => job.id === jobId && settle(job));
      const timer = setTimeout(
        () => done(new Error('preview render timed out')),
        RENDER_TIMEOUT_MS,
      );
      signal.addEventListener('abort', onAbort);
      jobs.watch(jobId);
      const known = jobs.byId[jobId];
      if (known) settle(known);
    });
  },

  cancel: (key) => cancelRequestApiV1PreviewRangeRequestsKeyDelete({ path: { key } }),

  async fetch(path, key, signal) {
    const { data } = await getVideoChunkApiV1ProjectPreviewRangeKeyGet({
      path: { key },
      query: { path },
      parseAs: 'blob',
      signal,
    });
    return data as unknown as Blob;
  },
};
