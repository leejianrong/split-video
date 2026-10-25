import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  client,
  configureClient,
  connectEvents,
  ifMatch,
  toApiError,
  whoamiApiV1WhoamiGet,
  type EventSourceLike,
  type EventsStatus,
} from './client';

// Node's Request cannot resolve a relative URL (a browser resolves it against the page), so tests use an origin.
const TEST_ORIGIN = 'http://localhost';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

/** Install a fetch mock; returns it, and a way to read the Request it was last called with. */
function mockFetch(respond: () => Response | Promise<Response>) {
  const fetchMock = vi.fn<(request: Request) => Promise<Response>>(() =>
    Promise.resolve(respond()),
  );
  configureClient({
    fetch: fetchMock as unknown as typeof fetch,
    token: null,
    baseUrl: TEST_ORIGIN,
  });
  return fetchMock;
}
const lastRequest = (fetchMock: ReturnType<typeof mockFetch>): Request => {
  const request = fetchMock.mock.calls.at(-1)?.[0];
  if (!request) throw new Error('fetch was not called');
  return request;
};

async function caught(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError);
    return error as ApiError;
  }
  throw new Error('expected the call to reject');
}

describe('requests', () => {
  it('builds URLs from the SDK path', async () => {
    const fetchMock = mockFetch(() => json({ id: 'local', kind: 'local', auth_required: false }));
    const { data } = await whoamiApiV1WhoamiGet();
    expect(data).toEqual({ id: 'local', kind: 'local', auth_required: false });
    expect(new URL(lastRequest(fetchMock).url).pathname).toBe('/api/v1/whoami');
  });

  it('honours a base URL', async () => {
    const fetchMock = mockFetch(() => json({}));
    configureClient({ baseUrl: 'https://example.test/prefix' });
    await whoamiApiV1WhoamiGet();
    expect(lastRequest(fetchMock).url).toBe('https://example.test/prefix/api/v1/whoami');
  });

  it('sends no Authorization header without a token, and a bearer header with one', async () => {
    const fetchMock = mockFetch(() => json({}));
    await whoamiApiV1WhoamiGet();
    expect(lastRequest(fetchMock).headers.get('Authorization')).toBeNull();
    configureClient({ token: 's3cret' });
    await whoamiApiV1WhoamiGet();
    expect(lastRequest(fetchMock).headers.get('Authorization')).toBe('Bearer s3cret');
    configureClient({ token: null });
    await whoamiApiV1WhoamiGet();
    expect(lastRequest(fetchMock).headers.get('Authorization')).toBeNull();
  });

  it('puts the revision in If-Match, quoted', async () => {
    const fetchMock = mockFetch(() => json({}));
    await whoamiApiV1WhoamiGet({ ...ifMatch(7) });
    expect(lastRequest(fetchMock).headers.get('If-Match')).toBe('"7"');
  });
});

describe('error mapping', () => {
  it('maps a catalogue body', async () => {
    mockFetch(() =>
      json(
        {
          detail: 'clip overlaps',
          code: 'clip.overlap',
          message: 'clip overlaps',
          fix: 'Move it.',
          details: { until: 12 },
        },
        422,
      ),
    );
    const error = await caught(whoamiApiV1WhoamiGet());
    expect(error).toMatchObject({
      code: 'clip.overlap',
      message: 'clip overlaps',
      fix: 'Move it.',
      status: 422,
    });
    expect(error.details).toEqual({ until: 12 });
    expect(error.isRevisionConflict).toBe(false);
  });

  it('recognises a 409 revision conflict and exposes the current revision', async () => {
    mockFetch(() =>
      json(
        {
          detail: 'project changed',
          code: 'revision.conflict',
          message: 'project changed',
          fix: 'Reload.',
          details: { current_revision: 9 },
          error: 'revision_conflict',
          current_revision: 9,
          base_revision: 7,
        },
        409,
      ),
    );
    const error = await caught(whoamiApiV1WhoamiGet());
    expect(error.isRevisionConflict).toBe(true);
    expect(error.currentRevision).toBe(9);
    expect(error.status).toBe(409);
  });

  it('recognises the older conflict body that has only `error`', () => {
    const error = toApiError(
      { detail: 'stale', error: 'revision_conflict', current_revision: 3 },
      409,
    );
    expect(error.isRevisionConflict).toBe(true);
    expect(error.currentRevision).toBe(3);
  });

  it('does not treat another 409 as a revision conflict', () => {
    const error = toApiError(
      { detail: 'exists', code: 'timeline.exists', message: 'exists', fix: 'Rename.' },
      409,
    );
    expect(error.isRevisionConflict).toBe(false);
    expect(error.code).toBe('timeline.exists');
  });

  it('maps the 401 body through its `error` field', () => {
    const error = toApiError({ detail: 'No valid bearer token', error: 'unauthorized' }, 401);
    expect(error).toMatchObject({
      code: 'unauthorized',
      message: 'No valid bearer token',
      status: 401,
    });
  });

  it('falls back for a body that is not the catalogue shape', async () => {
    mockFetch(() => new Response('<html>Bad gateway</html>', { status: 502 }));
    const error = await caught(whoamiApiV1WhoamiGet());
    expect(error.code).toBe('unknown');
    expect(error.status).toBe(502);
    expect(error.message).toContain('502');
    expect(error.fix).toBe('');
  });

  it('falls back for FastAPI list-valued detail without a message', () => {
    const error = toApiError({ detail: [{ loc: ['query', 'x'], msg: 'bad' }] }, 422);
    expect(error).toMatchObject({ code: 'unknown', status: 422 });
    expect(error.message).toContain('422');
  });

  it('maps a network failure to status 0 and code network', async () => {
    configureClient({
      fetch: (() => Promise.reject(new TypeError('Failed to fetch'))) as unknown as typeof fetch,
    });
    const error = await caught(whoamiApiV1WhoamiGet());
    expect(error).toMatchObject({ code: 'network', status: 0, message: 'Failed to fetch' });
    expect(error.fix).not.toBe('');
  });
});

class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  listeners = new Map<string, (event: MessageEvent<string>) => void>();
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.set(type, listener);
  }
  close() {
    this.closed = true;
  }
  emit(type: string, data: string, lastEventId = '') {
    this.listeners.get(type)?.({ data, lastEventId } as MessageEvent<string>);
  }
}

describe('connectEvents', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const start = (extra: Partial<Parameters<typeof connectEvents>[0]> = {}) => {
    const statuses: EventsStatus[] = [];
    const events: [string, unknown, string][] = [];
    const stop = connectEvents({
      onEvent: (name, data, id) => events.push([name, data, id]),
      onStatus: (status) => statuses.push(status),
      eventSource: (url) => new FakeEventSource(url),
      ...extra,
    });
    return { stop, statuses, events };
  };
  const latest = () => FakeEventSource.instances.at(-1)!;

  it('opens /api/v1/events with filters and delivers parsed frames', () => {
    const { events } = start({ query: { kind: ['job', 'project'], project: 'demo' } });
    const url = new URL(latest().url, 'http://localhost');
    expect(url.pathname).toBe('/api/v1/events');
    expect(url.searchParams.get('project')).toBe('demo');
    expect(url.searchParams.getAll('kind').join(',')).toContain('job');
    latest().emit('project', '{"revision":4}', '12');
    latest().emit('hello', 'not json');
    expect(events).toEqual([
      ['project', { revision: 4 }, '12'],
      ['hello', 'not json', ''],
    ]);
  });

  it('reconnects with exponential backoff, capped, and resets after an open', () => {
    const { statuses } = start({ initialDelayMs: 100, maxDelayMs: 350 });
    expect(FakeEventSource.instances).toHaveLength(1);

    latest().onerror?.(new Event('error'));
    expect(latest().closed).toBe(true);
    expect(statuses.at(-1)).toBe('reconnecting');
    vi.advanceTimersByTime(99);
    expect(FakeEventSource.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.instances).toHaveLength(2);

    latest().onerror?.(new Event('error')); // second failure: 200 ms
    vi.advanceTimersByTime(199);
    expect(FakeEventSource.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.instances).toHaveLength(3);

    latest().onerror?.(new Event('error')); // third: 400 capped to 350
    vi.advanceTimersByTime(349);
    expect(FakeEventSource.instances).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.instances).toHaveLength(4);

    latest().onopen?.(new Event('open'));
    expect(statuses.at(-1)).toBe('open');
    latest().onerror?.(new Event('error')); // backoff restarts at 100
    vi.advanceTimersByTime(100);
    expect(FakeEventSource.instances).toHaveLength(5);
  });

  it('flags an open after a drop as a reconnect', () => {
    const opens: boolean[] = [];
    start({
      onStatus: (status, info) => status === 'open' && opens.push(info.reconnect),
      initialDelayMs: 10,
    });
    latest().onopen?.(new Event('open'));
    latest().onerror?.(new Event('error'));
    vi.advanceTimersByTime(10);
    latest().onopen?.(new Event('open'));
    expect(opens).toEqual([false, true]);
  });

  it('stops reconnecting once closed', () => {
    const { stop, statuses } = start({ initialDelayMs: 100 });
    latest().onerror?.(new Event('error'));
    stop();
    vi.advanceTimersByTime(10_000);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(statuses.at(-1)).toBe('closed');
  });
});

describe('client', () => {
  it('uses same-origin relative URLs by default (empty base URL)', () => {
    configureClient({ baseUrl: '' });
    expect(client.buildUrl({ url: '/api/v1/jobs' })).toBe('/api/v1/jobs');
  });
});
