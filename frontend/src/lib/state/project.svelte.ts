import { ApiError, openFileApiV1OpenPost, type StateResponse } from '../api/client';

export type ProjectStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface ProjectError {
  code: string;
  message: string;
  fix: string;
  status: number;
}

/**
 * The open project: what the server says it is, and the revision it was read at (ADR 0004, "State"). Load only for
 * now; edits arrive with the timeline cards and will send `If-Match: <revision>`.
 *
 * The model is the video's timeline project (ADR 0003). Today the server opens it through `POST /api/v1/open`,
 * whose body is the video's path in the library, so the project id in the URL hash is that path.
 */
export class ProjectStore {
  status = $state<ProjectStatus>('idle');
  /** The id being opened or open (the path in the library); null before anything was opened. */
  id = $state<string | null>(null);
  project = $state<StateResponse | null>(null);
  error = $state<ProjectError | null>(null);

  /** The server revision the mirror was read at. */
  revision = $derived(this.project?.revision ?? null);
  /** The name shown in the top bar: the video's file name. */
  name = $derived(this.project?.filename ?? null);

  // Only the newest open() may write: a slow answer for an old id must not replace a newer project.
  #ticket = 0;

  async open(id: string): Promise<void> {
    const ticket = ++this.#ticket;
    this.id = id;
    this.status = 'loading';
    this.error = null;
    try {
      const { data } = await openFileApiV1OpenPost({ body: { path: id } });
      if (ticket !== this.#ticket) return;
      this.project = data;
      this.status = 'ready';
    } catch (cause) {
      if (ticket !== this.#ticket) return;
      this.project = null;
      this.error = toProjectError(cause);
      this.status = 'error';
    }
  }

  /** Forget the open project (back to the start or library views keep the last one unless asked). */
  close(): void {
    this.#ticket++;
    this.id = null;
    this.project = null;
    this.error = null;
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
