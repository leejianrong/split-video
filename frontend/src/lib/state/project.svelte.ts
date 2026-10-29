import {
  addAssetClipApiV1ProjectClipsPost,
  ApiError,
  applyVideoOpsApiV1ProjectOpsPost,
  getVideoTimelineApiV1ProjectTimelineGet,
  ifMatch,
  openFileApiV1OpenPost,
  type ProjectOut,
  type StateResponse,
} from '../api/client';
import type { ProjectEvent } from './jobs.svelte';

export type ProjectStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface ProjectError {
  code: string;
  message: string;
  fix: string;
  status: number;
}

/**
 * The open project: what the server says it is, and the revision it was read at (ADR 0004, "State"). Edits go through
 * `edit()`: one at a time, sent with `If-Match: <revision>`; a stale revision (409) reloads the project and reports a
 * conflict instead of overwriting (`addAsset` is the first edit, the timeline cards add the rest).
 *
 * The model is the video's timeline project (ADR 0003). Today the server opens it through `POST /api/v1/open`,
 * whose body is the video's path in the library, so the project id in the URL hash is that path.
 */
export class ProjectStore {
  status = $state<ProjectStatus>('idle');
  /** The id being opened or open (the path in the library); null before anything was opened. */
  id = $state<string | null>(null);
  project = $state<StateResponse | null>(null);
  /** The video's own timeline (tracks and clips), read with the project and after every edit. */
  timeline = $state<ProjectOut | null>(null);
  error = $state<ProjectError | null>(null);
  /** Why the last edit did not apply (a conflict or a refusal, with the fix hint); null after a good one. */
  editError = $state<ProjectError | null>(null);
  /**
   * Set when the last edit lost a race (409): the latest version is already loaded, and `retry()` sends the same
   * change again on top of it. Null after any later edit, open or close.
   */
  conflict = $state<{ retry: () => Promise<boolean> } | null>(null);

  /** The server revision the mirror was read at. The project and its timeline share one number; the larger is the newer. */
  revision = $derived(
    this.project === null && this.timeline === null
      ? null
      : Math.max(this.timeline?.revision ?? 0, this.project?.revision ?? 0),
  );
  /** The name shown in the top bar: the video's file name. */
  name = $derived(this.project?.filename ?? null);

  // Only the newest open() may write: a slow answer for an old id must not replace a newer project.
  #ticket = 0;
  // Edits are serialised: the next one starts from the revision the previous one returned.
  #queue: Promise<unknown> = Promise.resolve();

  async open(id: string): Promise<void> {
    const ticket = ++this.#ticket;
    this.id = id;
    this.status = 'loading';
    this.error = null;
    try {
      const { data } = await openFileApiV1OpenPost({ body: { path: id } });
      if (ticket !== this.#ticket) return;
      this.project = data;
      this.timeline = null;
      this.editError = null;
      this.conflict = null;
      this.status = 'ready';
      await this.#loadTimeline(id, ticket);
    } catch (cause) {
      if (ticket !== this.#ticket) return;
      this.project = null;
      this.timeline = null;
      this.error = toProjectError(cause);
      this.status = 'error';
    }
  }

  async #loadTimeline(id: string, ticket: number): Promise<void> {
    try {
      const { data } = await getVideoTimelineApiV1ProjectTimelineGet({ query: { path: id } });
      if (ticket === this.#ticket) this.timeline = data;
    } catch {
      // The editor works without the clip list; the next refresh() tries again.
    }
  }

  /** Read the project and its timeline again (after a job changed it, an agent edit, or a conflict). */
  async refresh(): Promise<void> {
    const id = this.id;
    if (id === null || this.status !== 'ready') return;
    const ticket = this.#ticket;
    try {
      const { data } = await openFileApiV1OpenPost({ body: { path: id } });
      if (ticket !== this.#ticket) return;
      this.project = data;
    } catch {
      return; // keep what we have; the caller's own error handling reports the cause
    }
    await this.#loadTimeline(id, ticket);
  }

  /**
   * A `project` event from the stream: reload when it is this project and newer than what we hold. Resolves with
   * whether the mirror was reloaded because of it (our own edits already hold their revision, so they do not).
   */
  async handleProjectEvent(event: ProjectEvent): Promise<boolean> {
    if (this.id === null || this.revision === null) return false;
    const mine = event.project_id === this.id || event.project_id === this.project?.filename;
    if (!mine || event.revision <= this.revision) return false;
    // An edit of ours may be in flight; let it land first so its answer is not mistaken for someone else's change.
    await this.#queue.catch(() => undefined);
    if (this.revision === null || event.revision <= this.revision) return false;
    await this.refresh();
    return true;
  }

  /**
   * Run one edit against the open project, based on the revision we hold. `change` gets `ifMatch(revision)` to
   * spread into the request and returns the new timeline. On a 409 the project is reloaded and `editError` says so
   * (the edit is not re-applied: the user decides); on any other failure `editError` carries the catalogue's fix.
   * Resolves with whether the edit applied.
   */
  edit(change: (match: ReturnType<typeof ifMatch>) => Promise<ProjectOut>): Promise<boolean> {
    const run = async (): Promise<boolean> => {
      if (this.id === null || this.revision === null) return false;
      this.editError = null;
      this.conflict = null;
      try {
        this.timeline = await change(ifMatch(this.revision));
        if (this.project) this.project = { ...this.project, revision: this.timeline.revision };
        return true;
      } catch (cause) {
        const error = toProjectError(cause);
        if (cause instanceof ApiError && cause.isRevisionConflict) {
          this.editError = {
            ...error,
            message: 'This project changed somewhere else, so your change was not applied.',
            fix: 'Fix: the latest version is loaded now; check it, then make the change again.',
          };
          this.conflict = { retry: () => this.edit(change) };
          await this.refresh();
        } else {
          this.editError = error;
        }
        return false;
      }
    };
    const result = this.#queue.then(run, run);
    this.#queue = result;
    return result;
  }

  /** Add the library asset as a clip on the main track (at `index`, default the end). */
  addAsset(assetId: string, index?: number): Promise<boolean> {
    const id = this.id;
    return this.edit(async (match) => {
      const { data } = await addAssetClipApiV1ProjectClipsPost({
        query: { path: id ?? '' },
        body: { asset: assetId, index: index ?? null },
        ...match,
      });
      return data;
    });
  }

  /**
   * Apply edit operations (docs/edit-ops.md: `trim_clip`, `move_clip`, `place_clip`, `set_track_flags`, `add_clip`, ...)
   * to the video's timeline, atomically, based on the revision we hold, then read the timeline again. Every edit of
   * the timeline UI goes through here, so history (undo) can wrap this one function later. Resolves with whether it
   * applied; on a refusal `editError` carries the engine's message and fix.
   */
  applyOps(ops: readonly Record<string, unknown>[]): Promise<boolean> {
    const id = this.id;
    return this.edit(async (match) => {
      await applyVideoOpsApiV1ProjectOpsPost({
        query: { path: id ?? '' },
        body: { schema_version: 1, ops: [...ops] },
        ...match,
      });
      const { data } = await getVideoTimelineApiV1ProjectTimelineGet({ query: { path: id ?? '' } });
      return data;
    });
  }

  /** Forget the open project (back to the start or library views keep the last one unless asked). */
  close(): void {
    this.#ticket++;
    this.id = null;
    this.project = null;
    this.timeline = null;
    this.error = null;
    this.editError = null;
    this.conflict = null;
    this.status = 'idle';
  }
}

export function toProjectError(cause: unknown): ProjectError {
  if (cause instanceof ApiError) {
    return { code: cause.code, message: cause.message, fix: cause.fix, status: cause.status };
  }
  return {
    code: 'unknown',
    message: cause instanceof Error ? cause.message : 'Something went wrong.',
    fix: '',
    status: 0,
  };
}

export const project = new ProjectStore();
