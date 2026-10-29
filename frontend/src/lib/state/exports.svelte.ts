import {
  exportModesRouteApiV1ComposeExportPost,
  listJobsApiV1JobsGet,
  renderQueueCancelApiV1ComposeRenderQueueBatchIdCancelPost,
  type SplitVideoEditorComposeExportRequest as ExportBody,
  type RenderQueueOut,
} from '../api/client';
import { buildRequest, DEFAULTS, plannedFiles, type ExportSettings } from '../export/plan';
import { SvelteMap } from 'svelte/reactivity';
import { announce } from '../a11y/announce';
import { jobs as defaultJobs, type Job, type JobsStore } from './jobs.svelte';
import {
  project as defaultProject,
  toProjectError,
  type ProjectError,
  type ProjectStore,
} from './project.svelte';

export type RowState = 'waiting' | 'rendering' | 'done' | 'failed' | 'cancelled';

/** One file of the render queue, as the panel shows it. */
export interface QueueRow {
  id: string;
  name: string;
  state: RowState;
  /** 0 to 1. */
  progress: number;
  /** Failed: what went wrong and what to try (the error catalogue's `message` and `fix`). */
  message: string;
  fix: string;
  /** The batch it was queued in (`group`): cancel all works on batches. */
  batch: string;
  /** Whether this page knows the request, so Retry can send the same file again. */
  retryable: boolean;
  /** Done: the folder it went to, when this page started it. */
  folder: string;
}

const STATES: Record<string, RowState> = {
  queued: 'waiting',
  running: 'rendering',
  done: 'done',
  error: 'failed',
  cancelled: 'cancelled',
};

export const isActive = (row: Pick<QueueRow, 'state'>): boolean =>
  row.state === 'waiting' || row.state === 'rendering';

/** The engine's fix hints talk about API flags; where the dialog has a control for it, say that instead. */
export function fixFor(code: string, fix: string): string {
  if (code === 'render.output_exists') {
    return 'Tick "Replace files that already exist" or pick another folder, then retry.';
  }
  return fix.replace(/`/g, '');
}

export function toRow(job: Job, retryable = false, folder = ''): QueueRow {
  const info = job.error_info ?? null;
  const fix = info ? fixFor(info.code ?? job.error_code ?? '', info.fix ?? '') : '';
  return {
    id: job.id,
    name: job.label || 'Output file',
    state: STATES[job.state] ?? 'failed',
    progress: job.fraction,
    message: info?.message || job.error || '',
    fix,
    batch: job.group ?? '',
    retryable,
    folder,
  };
}

/**
 * The export dialog's settings and the render queue (ux-spec #export). Starting an export is one engine call
 * (`POST /compose/export`); the rows come from the jobs store (`kind` `render`), which the event stream fills, so
 * the queue survives closing the dialog and reloading the page. Retry sends the same file again for the rows this
 * page started (the server keeps no request to replay).
 */
export class ExportStore {
  settings = $state<ExportSettings>({ ...DEFAULTS });
  starting = $state(false);
  /** Why the last start did not queue anything, with the fix hint. */
  error = $state<ProjectError | null>(null);

  /** Request and folder of every render this page queued, by job id. */
  #sent = new SvelteMap<string, { request: Record<string, unknown>; folder: string }>();
  /** Failed rows that were retried: the new row replaces them. */
  superseded = $state<Record<string, true>>({});

  #jobs: JobsStore;
  #project: ProjectStore;

  /** Newest batch first; within a batch, in the order the files were queued. */
  rows = $derived.by(() => {
    const renders = this.#jobs.list.filter((j) => j.kind === 'render' && !this.superseded[j.id]);
    // eslint-disable-next-line svelte/prefer-svelte-reactivity
    const newest = new Map<string, number>();
    for (const j of renders) {
      const key = j.group ?? j.id;
      newest.set(key, Math.max(newest.get(key) ?? 0, j.created_at));
    }
    return renders
      .sort(
        (a, b) =>
          (newest.get(b.group ?? b.id) ?? 0) - (newest.get(a.group ?? a.id) ?? 0) ||
          a.created_at - b.created_at,
      )
      .map((j) => toRow(j, this.#sent.has(j.id), this.#sent.get(j.id)?.folder ?? ''));
  });
  active = $derived(this.rows.filter(isActive));
  files = $derived.by(() => plannedFiles(this.#project.timeline, this.settings.mode));

  constructor(jobs: JobsStore = defaultJobs, project: ProjectStore = defaultProject) {
    this.#jobs = jobs;
    this.#project = project;
  }

  /** Load the queue now and after every reconnect; returns the stop function. */
  watch(): () => void {
    void this.load();
    return this.#jobs.onResync(() => void this.load());
  }

  /**
   * Read every render the server still remembers into the jobs store: the event stream's first frame lists only the
   * running ones, so this is what makes finished and failed rows survive a reload. Call once, and again on resync.
   */
  async load(): Promise<void> {
    try {
      const { data } = await listJobsApiV1JobsGet({ query: { kind: 'render' } });
      for (const job of data.jobs) this.#jobs.ingest(job);
    } catch {
      // The status line already says the server is unreachable; the queue shows what it has.
    }
  }

  /** Queue the export of the current settings. Returns false when nothing was queued. */
  async start(): Promise<boolean> {
    const id = this.#project.id;
    if (!id || this.starting) return false;
    const files = this.files;
    if (files.length === 0) return false;
    const request = buildRequest(id, this.settings, files);
    return this.#send(
      request,
      files.map((f) => f.number),
    );
  }

  /** Send one failed or cancelled file again; the old row goes away. */
  async retry(rowId: string): Promise<boolean> {
    const sent = this.#sent.get(rowId);
    if (!sent || this.starting) return false;
    const ok = await this.#send({ ...sent.request, overwrite: true }, null, rowId);
    return ok;
  }

  async #send(request: Record<string, unknown>, numbers: number[] | null, replaces?: string) {
    this.starting = true;
    this.error = null;
    try {
      const { data } = await exportModesRouteApiV1ComposeExportPost({
        body: request as ExportBody,
      });
      const folder = String(request.output_dir ?? '');
      await this.#adopt(data, request, numbers, folder, replaces);
      return true;
    } catch (cause) {
      this.error = toProjectError(cause);
      return false;
    } finally {
      this.starting = false;
    }
  }

  async #adopt(
    batch: RenderQueueOut,
    request: Record<string, unknown>,
    numbers: number[] | null,
    folder: string,
    replaces?: string,
  ) {
    // Per-file requests, so each row can be retried on its own: the batch lists jobs in the order of `only`.
    const only = Array.isArray(request.only) ? (request.only as number[]) : null;
    batch.jobs.forEach((entry, i) => {
      const one = only ? { ...request, only: [only[i] ?? numbers?.[i] ?? 0] } : request;
      this.#sent.set(entry.job_id, { request: one, folder });
    });
    if (replaces) this.superseded[replaces] = true;
    await Promise.all(batch.jobs.map((j) => this.#jobs.refresh(j.job_id)));
    announce(`Export started: ${batch.total} ${batch.total === 1 ? 'file' : 'files'}`);
  }

  /** Real cancel of one render: a queued one never starts, a running one has ffmpeg stopped. */
  async cancel(rowId: string): Promise<void> {
    try {
      await this.#jobs.cancel(rowId);
    } catch (cause) {
      this.error = toProjectError(cause);
    }
  }

  /** Cancel every queued and running render, batch by batch. */
  async cancelAll(): Promise<void> {
    // eslint-disable-next-line svelte/prefer-svelte-reactivity
    const batches = new Set(this.active.map((r) => r.batch).filter(Boolean));
    const loose = this.active.filter((r) => !r.batch);
    await Promise.all([
      ...[...batches].map(async (batch) => {
        try {
          await renderQueueCancelApiV1ComposeRenderQueueBatchIdCancelPost({
            path: { batch_id: batch },
          });
        } catch (cause) {
          this.error = toProjectError(cause);
        }
      }),
      ...loose.map((r) => this.cancel(r.id)),
    ]);
    // The cancel answer is the batch; the job frames arrive on the stream, and a refetch settles any that were missed.
    await Promise.all(this.rows.map((r) => this.#jobs.refresh(r.id)));
  }
}

export const exports = new ExportStore();
