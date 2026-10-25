import { announce } from '../a11y/announce';
import {
  getHighlightsApiV1AssetsAssetIdHighlightsGet,
  ifMatch,
  prepareReportApiV1AssetsAssetIdReportPreparePost,
  startSplitApiV1AssetsAssetIdSplitPost,
  startTranscriptionApiV1AssetsAssetIdTranscribePost,
  type AssetOut,
} from '../api/client';
import { jobs as defaultJobs, type Job, type JobsStore } from './jobs.svelte';
import {
  project as defaultProject,
  toProjectError,
  type ProjectError,
  type ProjectStore,
} from './project.svelte';

export type ActionId = 'split' | 'songs' | 'highlights' | 'transcribe';

export interface ActionDef {
  id: ActionId;
  label: string;
  /** What it does, in the words of the ux-spec; shown as the button's tooltip. */
  hint: string;
  /** The job kind it runs as. */
  kind: string;
  /** Verb phrase for announcements ("Splitting by silence"). */
  doing: string;
}

/** The four quick actions of the Media tab (docs/ux-spec.md#left-tabs), each an existing engine job. */
export const ACTIONS: readonly ActionDef[] = [
  {
    id: 'split',
    label: 'Split by silence',
    hint: 'Cut the recording at every long silence and label the pieces Part 1, Part 2 and so on.',
    kind: 'split',
    doing: 'Splitting by silence',
  },
  {
    id: 'songs',
    label: 'Find songs',
    hint: 'Cut the recording where the music stops and label the pieces Song 1, Song 2 and so on.',
    kind: 'split',
    doing: 'Finding songs',
  },
  {
    id: 'highlights',
    label: 'Find highlights',
    hint: 'Listen to the recording and suggest the moments most worth keeping.',
    kind: 'analysis',
    doing: 'Finding highlights',
  },
  {
    id: 'transcribe',
    label: 'Transcribe',
    hint: 'Write down what is said, in the background. Needs the optional transcription add-on.',
    kind: 'transcription',
    doing: 'Transcribing',
  },
];

export const actionById = (id: ActionId): ActionDef => ACTIONS.find((a) => a.id === id)!;

/** One run of one action on one asset. The job's progress lives in the jobs store; this is the link to it. */
export interface Run {
  key: string;
  assetId: string;
  assetPath: string;
  action: ActionId;
  /** The job, once the server accepted the request. */
  jobId: string | null;
  submitting: boolean;
  /** A refusal before any job existed (the asset is missing, the add-on is not installed, a stale revision). */
  error: ProjectError | null;
  /** What it produced, in words, once done ("3 songs found"). */
  summary: string | null;
  /** Finishing step after the job (reading the highlights it made). */
  finishing: boolean;
}

export type RunState =
  'idle' | 'submitting' | 'queued' | 'running' | 'done' | 'error' | 'cancelled';

export interface RunView {
  state: RunState;
  /** 0 to 1, or null while the amount of work is not known. */
  fraction: number | null;
  message: string | null;
  error: ProjectError | null;
  summary: string | null;
  cancelling: boolean;
}

const keyOf = (assetId: string, action: ActionId) => `${assetId}:${action}`;

/** The error a failed job carries (`error_info` is `{code, message, fix}`), or the bare message. */
export function jobError(job: Job): ProjectError {
  const info = job.error_info;
  return {
    code: info?.code ?? job.error_code ?? 'error.internal',
    message: info?.message ?? job.error ?? 'The job failed.',
    fix: info?.fix ?? '',
    status: 0,
  };
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export class QuickActionsStore {
  runs = $state<Record<string, Run>>({});

  #jobs: JobsStore;
  #project: ProjectStore;

  constructor(jobs: JobsStore = defaultJobs, project: ProjectStore = defaultProject) {
    this.#jobs = jobs;
    this.#project = project;
    jobs.onFinished((job) => void this.#finished(job));
  }

  runOf(assetId: string, action: ActionId): Run | undefined {
    return this.runs[keyOf(assetId, action)];
  }

  /** What to draw for this run: the job's live state when there is one. */
  view(assetId: string, action: ActionId): RunView {
    const run = this.runOf(assetId, action);
    if (!run)
      return {
        state: 'idle',
        fraction: null,
        message: null,
        error: null,
        summary: null,
        cancelling: false,
      };
    if (run.error)
      return {
        state: 'error',
        fraction: null,
        message: null,
        error: run.error,
        summary: null,
        cancelling: false,
      };
    const job = run.jobId ? this.#jobs.byId[run.jobId] : undefined;
    if (!job) {
      return {
        state: run.submitting ? 'submitting' : 'queued',
        fraction: null,
        message: null,
        error: null,
        summary: null,
        cancelling: false,
      };
    }
    if (job.state === 'done') {
      return {
        state: run.finishing ? 'running' : 'done',
        fraction: run.finishing ? null : 1,
        message: run.finishing ? 'Reading the results' : null,
        error: null,
        summary: run.summary,
        cancelling: false,
      };
    }
    if (job.state === 'error') {
      return {
        state: 'error',
        fraction: null,
        message: null,
        error: jobError(job),
        summary: null,
        cancelling: false,
      };
    }
    if (job.state === 'cancelled') {
      return {
        state: 'cancelled',
        fraction: null,
        message: null,
        error: null,
        summary: null,
        cancelling: false,
      };
    }
    return {
      state: job.state === 'queued' ? 'queued' : 'running',
      fraction: job.total > 0 ? Math.min(Math.max(job.fraction, 0), 1) : null,
      message: job.message ?? null,
      error: null,
      summary: null,
      cancelling: job.cancel_requested === true,
    };
  }

  isBusy(assetId: string, action: ActionId): boolean {
    const { state } = this.view(assetId, action);
    return state === 'submitting' || state === 'queued' || state === 'running';
  }

  /** Start `action` on `asset`. Resolves with whether a job was started. */
  async run(action: ActionId, asset: AssetOut): Promise<boolean> {
    const key = keyOf(asset.id, action);
    if (this.isBusy(asset.id, action)) return false;
    const def = actionById(action);
    const run: Run = {
      key,
      assetId: asset.id,
      assetPath: asset.path,
      action,
      jobId: null,
      submitting: true,
      error: null,
      summary: null,
      finishing: false,
    };
    this.runs[key] = run;
    try {
      const jobId = await this.#start(action, asset);
      this.runs[key].jobId = jobId;
      this.#jobs.watch(jobId);
      await this.#jobs.refresh(jobId);
      announce(`${def.doing} ${asset.path}`);
      return true;
    } catch (cause) {
      this.runs[key].error = toProjectError(cause);
      announce(
        `${def.label} could not start. ${this.runs[key].error?.message ?? ''} ${this.runs[key].error?.fix ?? ''}`,
      );
      return false;
    } finally {
      this.runs[key].submitting = false;
    }
  }

  async #start(action: ActionId, asset: AssetOut): Promise<string> {
    const path = { asset_id: asset.id };
    if (action === 'split' || action === 'songs') {
      // Saving the cuts replaces the splits of the open project, so it is based on the revision we hold.
      const open = this.#project.id === asset.path && this.#project.revision !== null;
      const { data } = await startSplitApiV1AssetsAssetIdSplitPost({
        path,
        body: { label_prefix: action === 'songs' ? 'Song' : 'Part' },
        ...(open ? ifMatch(this.#project.revision as number) : {}),
      });
      return data.job_id;
    }
    if (action === 'highlights') {
      const { data } = await prepareReportApiV1AssetsAssetIdReportPreparePost({ path });
      return data.job_id;
    }
    const { data } = await startTranscriptionApiV1AssetsAssetIdTranscribePost({ path, body: {} });
    return data.job_id;
  }

  /** Cancel for real: the server stops the job. */
  async cancel(assetId: string, action: ActionId): Promise<void> {
    const run = this.runOf(assetId, action);
    if (!run?.jobId) return;
    try {
      const job = await this.#jobs.cancel(run.jobId);
      if (job.state === 'cancelled') announce(`${actionById(action).label} cancelled`);
    } catch (cause) {
      run.error = toProjectError(cause);
    }
  }

  dismiss(assetId: string, action: ActionId): void {
    delete this.runs[keyOf(assetId, action)];
  }

  async #finished(job: Job): Promise<void> {
    const run = Object.values(this.runs).find((r) => r.jobId === job.id);
    if (!run) return;
    const def = actionById(run.action);
    if (job.state === 'error') {
      const error = jobError(job);
      announce(`${def.label} failed. ${error.message} ${error.fix}`);
      return;
    }
    if (job.state === 'cancelled') return;
    const result = (job.result ?? {}) as Record<string, unknown>;
    if (run.action === 'split' || run.action === 'songs') {
      const count = typeof result.segments === 'number' ? result.segments : 0;
      run.summary =
        count > 1
          ? `${plural(count, run.action === 'songs' ? 'song' : 'part')} found`
          : 'No silence long enough to cut at';
      // The cuts changed the open project: read it again so the timeline shows them.
      if (this.#project.id === run.assetPath) await this.#project.refresh();
    } else if (run.action === 'highlights') {
      run.finishing = true;
      try {
        const { data } = await getHighlightsApiV1AssetsAssetIdHighlightsGet({
          path: { asset_id: run.assetId },
        });
        run.summary = `${plural(data.candidates.length, 'highlight')} found`;
      } catch (cause) {
        run.error = toProjectError(cause);
        announce(`${def.label} failed. ${run.error.message} ${run.error.fix}`);
        return;
      } finally {
        run.finishing = false;
      }
    } else {
      run.summary = 'Transcript ready';
    }
    announce(`${def.label} finished: ${run.summary}`);
  }
}

export const quickActions = new QuickActionsStore();
