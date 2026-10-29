import type { AssetOut } from '../api/client';
import { assets as defaultAssets, type AssetsStore } from './assets.svelte';
import { jobs as defaultJobs, type JobsStore } from './jobs.svelte';
import { quickActions as defaultQuick, type QuickActionsStore } from './quickActions.svelte';
import type { ProjectError } from './project.svelte';
import type { ActionId } from './quickActions.svelte';
import type { TabId } from './layout';

/** What the user wants made (docs/ux-spec.md#first-run, step 2). */
export type Goal = 'songs' | 'highlights' | 'shorts' | 'empty';

export interface GoalDef {
  id: Goal;
  title: string;
  text: string;
  /** The quick action it starts (none for an empty timeline). */
  action: ActionId | null;
  /** The editor tab it opens on. */
  tab: TabId;
}

export const GOALS: readonly GoalDef[] = [
  {
    id: 'songs',
    title: 'Split into songs',
    text: 'Finds the gaps between songs and cuts one clip per song.',
    action: 'songs',
    tab: 'media',
  },
  {
    id: 'highlights',
    title: 'Find highlights',
    text: 'Suggests the best moments to review and keep.',
    action: 'highlights',
    tab: 'highlights',
  },
  {
    id: 'shorts',
    title: 'Make vertical shorts',
    text: 'Finds highlights to turn into 9:16 shorts, with captions when transcription is on.',
    action: 'highlights',
    tab: 'highlights',
  },
  {
    id: 'empty',
    title: 'Start with an empty timeline',
    text: 'Do it yourself: cut, trim and arrange clips.',
    action: null,
    tab: 'media',
  },
];

export const goalById = (id: Goal): GoalDef => GOALS.find((g) => g.id === id)!;

export type StepState = 'waiting' | 'working' | 'done' | 'failed';

export interface Step {
  id: 'analysis' | 'preview' | 'transcript';
  /** "Listening for silences and music": what is happening, in the words of the ux-spec. */
  label: string;
  state: StepState;
  /** 0 to 1, or null when unknown. */
  fraction: number | null;
  /** What it produced ("3 songs found"). */
  summary: string | null;
  error: ProjectError | null;
  optional: boolean;
}

const ANALYSIS_LABEL: Record<Goal, string> = {
  songs: 'Listening for silences and music',
  highlights: 'Listening for the best moments',
  shorts: 'Listening for the best moments',
  empty: '',
};

/**
 * The first-run page's state (ux-spec #first-run): the chosen recording, the goal, and the preparation steps. It
 * adds no engine behaviour: the goal starts the same quick-action job the Media tab does, and the preview is the
 * same `proxy` job. Progress is read from the jobs store, so it carries on after the editor opens.
 */
export class FirstRunStore {
  asset = $state<AssetOut | null>(null);
  goal = $state<Goal | null>(null);
  transcribe = $state(false);
  started = $state(false);

  #assets: AssetsStore;
  #jobs: JobsStore;
  #quick: QuickActionsStore;

  constructor(
    assets: AssetsStore = defaultAssets,
    jobs: JobsStore = defaultJobs,
    quick: QuickActionsStore = defaultQuick,
  ) {
    this.#assets = assets;
    this.#jobs = jobs;
    this.#quick = quick;
  }

  /** The recording as the library knows it now (the proxy flag changes when the preview is ready). */
  get current(): AssetOut | null {
    return this.asset ? (this.#assets.byId(this.asset.id) ?? this.asset) : null;
  }

  /** Use this recording. Starts the goal when one was already chosen. */
  setAsset(asset: AssetOut): void {
    this.asset = asset;
    void this.#startIfReady();
  }

  /** Back to step 1 (only before anything was started). */
  clear(): void {
    if (this.started) return;
    this.asset = null;
  }

  /** Choose the goal. It cannot change once started: the Media tab runs the other actions later. */
  choose(goal: Goal): void {
    if (this.started) return;
    this.goal = goal;
    this.transcribe = goal === 'shorts';
    void this.#startIfReady();
  }

  reset(): void {
    this.asset = null;
    this.goal = null;
    this.transcribe = false;
    this.started = false;
  }

  async #startIfReady(): Promise<void> {
    const asset = this.current;
    if (!asset || !this.goal || this.started) return;
    this.started = true;
    const action = goalById(this.goal).action;
    if (action) await this.#quick.run(action, asset);
    if (this.transcribe) await this.#quick.run('transcribe', asset);
  }

  /** Try a failed step again. */
  async retry(step: Step['id']): Promise<void> {
    const asset = this.current;
    if (!asset) return;
    if (step === 'preview') {
      await this.#assets.prepare(asset);
    } else if (step === 'transcript') {
      this.#quick.dismiss(asset.id, 'transcribe');
      await this.#quick.run('transcribe', asset);
    } else if (this.goal) {
      const action = goalById(this.goal).action;
      if (action) {
        this.#quick.dismiss(asset.id, action);
        await this.#quick.run(action, asset);
      }
    }
  }

  /** The steps to show, in order. Empty until a recording is chosen. */
  get steps(): Step[] {
    const asset = this.current;
    if (!asset) return [];
    const out: Step[] = [];
    const goal = this.goal ? goalById(this.goal) : null;
    if (goal?.action && this.started) {
      const view = this.#quick.view(asset.id, goal.action);
      out.push({
        id: 'analysis',
        label: ANALYSIS_LABEL[goal.id],
        ...fromRun(view),
        optional: false,
      });
    }
    out.push(this.#previewStep(asset));
    if (this.transcribe && this.started) {
      const view = this.#quick.view(asset.id, 'transcribe');
      out.push({
        id: 'transcript',
        label: 'Transcribing speech (optional, runs in the background)',
        ...fromRun(view),
        optional: true,
      });
    }
    return out;
  }

  #previewStep(asset: AssetOut): Step {
    const base = { id: 'preview' as const, optional: false, summary: null, error: null };
    const label = 'Building a fast preview';
    if (asset.proxy || !asset.has_video) {
      return { ...base, label, state: 'done', fraction: 1 };
    }
    const job = this.#jobs.latest('proxy', asset.path);
    if (!job) return { ...base, label, state: 'waiting', fraction: null };
    if (job.state === 'done') return { ...base, label, state: 'done', fraction: 1 };
    if (job.state === 'error' || job.state === 'cancelled') {
      return {
        ...base,
        label,
        state: 'failed',
        fraction: null,
        error: {
          code: job.error_info?.code ?? 'error.internal',
          message: job.error_info?.message ?? job.error ?? 'The preview could not be built.',
          fix: job.error_info?.fix ?? '',
          status: 0,
        },
      };
    }
    return {
      ...base,
      label,
      state: job.state === 'queued' ? 'waiting' : 'working',
      fraction: job.total > 0 ? Math.min(Math.max(job.fraction, 0), 1) : null,
    };
  }

  /** The editor can open once the preview is usable (built, or it failed and the original plays). */
  get canOpen(): boolean {
    const preview = this.steps.find((s) => s.id === 'preview');
    return preview !== undefined && (preview.state === 'done' || preview.state === 'failed');
  }
}

function fromRun(view: ReturnType<QuickActionsStore['view']>) {
  const state: StepState =
    view.state === 'done'
      ? 'done'
      : view.state === 'error' || view.state === 'cancelled'
        ? 'failed'
        : view.state === 'idle' || view.state === 'queued' || view.state === 'submitting'
          ? 'waiting'
          : 'working';
  return { state, fraction: view.fraction, summary: view.summary, error: view.error };
}

export const firstRun = new FirstRunStore();
