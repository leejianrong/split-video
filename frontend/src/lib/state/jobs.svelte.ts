import {
  cancelApiV1JobsJobIdCancelPost,
  connectEvents,
  getJobApiV1JobsJobIdGet,
  listJobsApiV1JobsGet,
  type EventName,
  type EventsOptions,
  type EventsStatus,
  type JobOut,
} from '../api/client';

export type Job = JobOut;

export const isFinished = (job: Pick<Job, 'state'>): boolean =>
  job.state === 'done' || job.state === 'error' || job.state === 'cancelled';

/** The payload of a `project` event (docs/api.md, `GET /api/v1/events`). */
export interface ProjectEvent {
  kind: string;
  project_id: string;
  revision: number;
  source: string;
}

type Connect = (options: EventsOptions) => () => void;

/** Finished jobs kept in memory; older ones are dropped (running ones never are). */
const KEEP_FINISHED = 200;

/**
 * Every job the server knows, mirrored from `GET /api/v1/events` (ADR 0004, "State"). This store owns the one event
 * connection of the page: `start()` opens it (reconnecting with backoff, see `connectEvents`), `hello` and `job`
 * frames fill `byId`, and after a reconnect the list is fetched again, because frames sent while the stream was down
 * are gone. The browser never polls. Other stores subscribe instead of opening their own stream:
 * `onFinished` (a job that was running or watched ended), `onProject` (a project's revision moved) and `onResync`
 * (the stream came back: refetch what you mirror).
 */
export class JobsStore {
  byId = $state<Record<string, Job>>({});
  status = $state<EventsStatus>('closed');

  /** Newest first. */
  list = $derived(Object.values(this.byId).sort((a, b) => b.created_at - a.created_at));

  #connect: Connect;
  #stop: (() => void) | null = null;
  // Plain bookkeeping, not UI state: nothing renders from these.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #watching = new Set<string>();
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #finished = new Set<(job: Job) => void>();
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #project = new Set<(event: ProjectEvent) => void>();
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  #resync = new Set<() => void>();

  constructor(connect: Connect = connectEvents) {
    this.#connect = connect;
  }

  /** Open the event stream (once). */
  start(): void {
    if (this.#stop) return;
    this.#stop = this.#connect({
      onEvent: (name, data) => this.#onEvent(name, data),
      onStatus: (status, info) => {
        this.status = status;
        if (status === 'open' && info.reconnect) void this.resync();
      },
    });
  }

  stop(): void {
    this.#stop?.();
    this.#stop = null;
  }

  /** Add or update a job from any source (an event, a start response, a refetch). */
  ingest(job: Job): void {
    const current = this.byId[job.id];
    // Frames can arrive out of order across a reconnect: never move a finished job back to running.
    if (current && isFinished(current) && !isFinished(job)) return;
    this.byId[job.id] = job;
    if (!isFinished(job)) {
      this.#watching.add(job.id);
      return;
    }
    if (this.#watching.delete(job.id)) for (const listener of this.#finished) listener(job);
    this.#prune();
  }

  /** Make sure `onFinished` fires for this job even if it was first seen already finished. */
  watch(id: string): void {
    this.#watching.add(id);
  }

  async refresh(id: string): Promise<Job | null> {
    try {
      const { data } = await getJobApiV1JobsJobIdGet({ path: { job_id: id } });
      this.ingest(data);
      return data;
    } catch {
      return null;
    }
  }

  /** Real cancel: the server stops the work (ffmpeg is terminated) and answers with the job's new state. */
  async cancel(id: string): Promise<Job> {
    const { data } = await cancelApiV1JobsJobIdCancelPost({ path: { job_id: id } });
    this.ingest(data);
    return data;
  }

  /** Fetch every job again (after a reconnect) and tell subscribers to refetch what they mirror. */
  async resync(): Promise<void> {
    try {
      const { data } = await listJobsApiV1JobsGet();
      for (const job of data.jobs) this.ingest(job);
    } catch {
      // The status line says the connection is down; the next successful open resyncs again.
    }
    for (const listener of this.#resync) listener();
  }

  onFinished(listener: (job: Job) => void): () => void {
    this.#finished.add(listener);
    return () => this.#finished.delete(listener);
  }

  onProject(listener: (event: ProjectEvent) => void): () => void {
    this.#project.add(listener);
    return () => this.#project.delete(listener);
  }

  onResync(listener: () => void): () => void {
    this.#resync.add(listener);
    return () => this.#resync.delete(listener);
  }

  /** The newest job of `kind` whose label is `label` (the asset's path for the jobs the Media tab starts). */
  latest(kind: string, label: string): Job | undefined {
    return this.list.find((j) => j.kind === kind && j.label === label);
  }

  #onEvent(name: EventName, data: unknown): void {
    if (typeof data !== 'object' || data === null) return;
    const body = data as Record<string, unknown>;
    if (name === 'hello') {
      const jobs = Array.isArray(body.jobs) ? (body.jobs as Job[]) : [];
      for (const job of jobs) this.ingest(job);
      if (body.resync === true) for (const listener of this.#resync) listener();
    } else if (name === 'job') {
      if (typeof body.job === 'object' && body.job !== null) this.ingest(body.job as Job);
    } else if (name === 'project') {
      for (const listener of this.#project) listener(body as unknown as ProjectEvent);
    }
  }

  #prune(): void {
    const finished = this.list.filter(isFinished);
    for (const job of finished.slice(KEEP_FINISHED)) delete this.byId[job.id];
  }
}

export const jobs = new JobsStore();
