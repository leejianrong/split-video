// Hand-written layer over the generated client in ./generated (see docs/frontend.md and ADR 0004, "API client").
// Components import SDK functions from here (or ./generated) and never call `fetch` themselves.
import { client } from './generated/client.gen';
import type { AssetOut } from './generated/types.gen';

export * from './generated/sdk.gen';
export type * from './generated/types.gen';
export { client };

// ---------------------------------------------------------------------------------------------------------
// Errors

/**
 * Every failed request rejects with this (the generator is set to `throwOnError: true`). The fields are the error
 * catalogue's (docs/errors.md): branch on `code`, show `message`, offer `fix`.
 */
export class ApiError extends Error {
  readonly code: string;
  readonly fix: string;
  /** HTTP status, or 0 when no response arrived (server down, offline). */
  readonly status: number;
  readonly details: Record<string, unknown> | null;
  /** The revision the server holds, on a 409 revision conflict. */
  readonly currentRevision: number | null;

  constructor(init: {
    code: string;
    message: string;
    fix?: string;
    status: number;
    details?: Record<string, unknown> | null;
    currentRevision?: number | null;
  }) {
    super(init.message);
    this.name = 'ApiError';
    this.code = init.code;
    this.fix = init.fix ?? '';
    this.status = init.status;
    this.details = init.details ?? null;
    this.currentRevision = init.currentRevision ?? null;
  }

  /** The write was based on a stale revision and nothing was written (409 `revision.conflict`). */
  get isRevisionConflict(): boolean {
    return (
      this.status === 409 &&
      (this.code === 'revision.conflict' || this.code === 'revision_conflict')
    );
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const str = (value: unknown): string | undefined =>
  typeof value === 'string' && value ? value : undefined;

/**
 * Map a response body (already JSON-parsed, or the raw text) to an ApiError. Bodies are the catalogue's
 * `{detail, code, message, fix, details}`, the 401 `{detail, error: "unauthorized"}`, the older 409
 * `{detail, error: "revision_conflict", current_revision}`, or anything else (a proxy's HTML page, FastAPI's
 * list-valued `detail`), which falls back to `code: "unknown"` and a message built from the status.
 */
export function toApiError(body: unknown, status: number): ApiError {
  const fallbackMessage =
    status > 0 ? `The server answered with HTTP ${status}.` : 'The server could not be reached.';
  if (!isRecord(body)) {
    // A non-JSON body (a proxy's HTML error page, an empty reply) is not worth showing; the status says enough.
    return new ApiError({
      code: status > 0 ? 'unknown' : 'network',
      message: fallbackMessage,
      status,
    });
  }
  const details = isRecord(body.details) ? body.details : null;
  const current = body.current_revision ?? details?.current_revision;
  // An edit batch the engine refused (422 `ops.invalid`): the first problem says what is wrong and what to do.
  const problem = Array.isArray(body.errors) && isRecord(body.errors[0]) ? body.errors[0] : null;
  const hint = problem ? str(problem.hint) : undefined;
  return new ApiError({
    code: str(body.code) ?? str(body.error) ?? 'unknown',
    message:
      (problem && str(problem.message)) ?? str(body.message) ?? str(body.detail) ?? fallbackMessage,
    fix: problem ? (hint ? `Fix: ${hint}` : str(body.fix)) : str(body.fix),
    status,
    details: problem ? { ...details, problem: String(problem.code ?? '') } : details,
    currentRevision: typeof current === 'number' ? current : null,
  });
}

// The generated client's error interceptor sees the parsed body (or the thrown fetch error) and the response.
client.interceptors.error.use((error, response) => {
  if (error instanceof ApiError) return error;
  if (error instanceof DOMException && error.name === 'AbortError') return error;
  if (response) return toApiError(error, response.status);
  return new ApiError({
    code: 'network',
    message:
      error instanceof Error && error.message ? error.message : 'The server could not be reached.',
    fix: 'Check that split-video is still running, then try again.',
    status: 0,
  });
});

// ---------------------------------------------------------------------------------------------------------
// Auth and revisions

let bearerToken: string | null = null;

// Browsers cannot attach this to `EventSource` or `<video src>`, see docs/api.md ("Known limitation").
client.interceptors.request.use((request) => {
  if (bearerToken) request.headers.set('Authorization', `Bearer ${bearerToken}`);
  return request;
});

export interface ClientSettings {
  /** Sent as `Authorization: Bearer ...` when set; `null` clears it. */
  token?: string | null;
  /** Prefix for every URL; defaults to same origin. */
  baseUrl?: string;
  /** A fetch implementation, for tests. */
  fetch?: typeof fetch;
}

export function configureClient(settings: ClientSettings): void {
  if (settings.token !== undefined) bearerToken = settings.token;
  const config: { baseUrl?: string; fetch?: typeof fetch } = {};
  if (settings.baseUrl !== undefined) config.baseUrl = settings.baseUrl;
  if (settings.fetch !== undefined) config.fetch = settings.fetch;
  if (Object.keys(config).length > 0) client.setConfig(config);
}

/**
 * `If-Match` for a write based on `revision` (the server accepts it quoted, as HTTP wants). Spread into a write's
 * options: `removeClip...({ path, ...ifMatch(project.revision) })`. Without it the write applies to the latest.
 */
export function ifMatch(revision: number): { headers: { 'If-Match': string } } {
  return { headers: { 'If-Match': `"${revision}"` } };
}

// ---------------------------------------------------------------------------------------------------------
// Server-sent events (GET /api/v1/events)

/** The frames the server sends (see the route description in openapi.json). Payloads are JSON. */
export const EVENT_NAMES = ['hello', 'job', 'project', 'overflow', 'shutdown'] as const;
export type EventName = (typeof EVENT_NAMES)[number];

export type EventsStatus = 'connecting' | 'open' | 'reconnecting' | 'closed';

/** The part of the browser's EventSource that is used, so tests can inject a fake. */
export interface EventSourceLike {
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(): void;
}
export type EventSourceFactory = (url: string) => EventSourceLike;

export interface EventsOptions {
  onEvent: (name: EventName, data: unknown, lastEventId: string) => void;
  /** `reconnect` is true for an open after a drop: refetch what you mirror, as events in between may be lost. */
  onStatus?: (status: EventsStatus, info: { reconnect: boolean; attempt: number }) => void;
  /** Server-side filters: `kind` (job, project), `job`, `project`. */
  query?: { kind?: string[]; job?: string; project?: string };
  /** Defaults to `new EventSource(url)`. */
  eventSource?: EventSourceFactory;
  /** Reconnect delay is `min(initialDelayMs * 2 ** attempt, maxDelayMs)`; the attempt counter resets on open. */
  initialDelayMs?: number;
  maxDelayMs?: number;
}

/** Open the event stream and keep it open (reconnect with exponential backoff). Returns a function that closes it. */
export function connectEvents(options: EventsOptions): () => void {
  const { onEvent, onStatus, query, initialDelayMs = 500, maxDelayMs = 15_000 } = options;
  const make: EventSourceFactory = options.eventSource ?? ((url) => new EventSource(url));
  const url = client.buildUrl({ url: '/api/v1/events', query });

  let source: EventSourceLike | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  let closed = false;
  let everOpened = false;

  const open = () => {
    timer = null;
    const current = make(url);
    source = current;
    for (const name of EVENT_NAMES) {
      current.addEventListener(name, (event) => {
        let data: unknown = event.data;
        try {
          data = JSON.parse(event.data);
        } catch {
          // keep the raw text
        }
        onEvent(name, data, event.lastEventId);
      });
    }
    current.onopen = () => {
      onStatus?.('open', { reconnect: everOpened, attempt });
      everOpened = true;
      attempt = 0;
    };
    current.onerror = () => {
      // The browser would retry by itself only for some failures; closing and retrying ourselves makes the
      // delay and the visible "reconnecting" state the same for all of them.
      current.close();
      if (closed || source !== current) return;
      const delay = Math.min(initialDelayMs * 2 ** attempt, maxDelayMs);
      attempt += 1;
      onStatus?.('reconnecting', { reconnect: true, attempt });
      timer = setTimeout(open, delay);
    };
  };

  onStatus?.('connecting', { reconnect: false, attempt: 0 });
  open();

  return () => {
    closed = true;
    if (timer !== null) clearTimeout(timer);
    source?.close();
    source = null;
    onStatus?.('closed', { reconnect: false, attempt });
  };
}

// ---------------------------------------------------------------------------------------------------------
// Uploads (POST /api/v1/assets/upload)

/** The part of XMLHttpRequest the upload uses, so tests can inject a fake. */
export interface XhrLike {
  upload: { onprogress: ((event: { loaded: number; total: number }) => void) | null };
  onload: (() => void) | null;
  onerror: (() => void) | null;
  onabort: (() => void) | null;
  status: number;
  responseText: string;
  open(method: string, url: string): void;
  setRequestHeader(name: string, value: string): void;
  send(body: Blob): void;
  abort(): void;
}

export interface UploadOptions {
  /** The workspace folder to put the file in; empty is the root. */
  folder?: string;
  /** Called while the bytes go out. `total` is the file's size. */
  onProgress?: (loaded: number, total: number) => void;
  signal?: AbortSignal;
  /** Defaults to `new XMLHttpRequest()`. `fetch` cannot report upload progress, so an upload uses XHR. */
  xhr?: () => XhrLike;
}

/**
 * Send `file` as the raw body of `POST /api/v1/assets/upload` and resolve with the new asset. The server writes it
 * into the workspace through the MediaStore, probes it and registers it. Rejects with `ApiError` (the catalogue's
 * `code`, `message` and `fix`); an abort rejects with `code: "cancelled"`.
 */
export function uploadFile(file: File, options: UploadOptions = {}): Promise<AssetOut> {
  const url = client.buildUrl({
    url: '/api/v1/assets/upload',
    query: { name: file.name, folder: options.folder ?? '' },
  });
  const xhr = options.xhr ? options.xhr() : (new XMLHttpRequest() as unknown as XhrLike);
  return new Promise<AssetOut>((resolve, reject) => {
    const abort = () => xhr.abort();
    const fail = (error: ApiError) => {
      options.signal?.removeEventListener('abort', abort);
      reject(error);
    };
    xhr.open('POST', url);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    if (bearerToken) xhr.setRequestHeader('Authorization', `Bearer ${bearerToken}`);
    xhr.upload.onprogress = (event) => options.onProgress?.(event.loaded, event.total || file.size);
    xhr.onload = () => {
      options.signal?.removeEventListener('abort', abort);
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // not JSON (a proxy's page): toApiError builds the message from the status
      }
      if (xhr.status >= 200 && xhr.status < 300 && isRecord(body)) {
        resolve(body as unknown as AssetOut);
      } else {
        reject(toApiError(body, xhr.status));
      }
    };
    xhr.onerror = () =>
      fail(
        new ApiError({
          code: 'network',
          message: 'The server could not be reached.',
          fix: 'Check that split-video is still running, then import the file again.',
          status: 0,
        }),
      );
    xhr.onabort = () =>
      fail(new ApiError({ code: 'cancelled', message: 'The import was cancelled.', status: 0 }));
    if (options.signal?.aborted) {
      xhr.onabort();
      return;
    }
    options.signal?.addEventListener('abort', abort, { once: true });
    xhr.send(file);
  });
}
