import { describe, expect, it, vi } from 'vitest';

const connect = vi.fn();
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  connectEvents: (...args: unknown[]) => connect(...args),
}));

import type { EventsOptions } from '../api/client';
import { connectLive } from './live';
import { jobs } from './jobs.svelte';
import { project } from './project.svelte';

describe('connectLive', () => {
  it('opens the one event stream, routes project events to the project, and closes with it', () => {
    const close = vi.fn();
    let options: EventsOptions | null = null;
    connect.mockImplementation((o: EventsOptions) => {
      options = o;
      return close;
    });
    const handle = vi.spyOn(project, 'handleProjectEvent').mockImplementation(() => undefined);
    const stop = connectLive();
    expect(connect).toHaveBeenCalledTimes(1);
    (options as EventsOptions | null)?.onEvent(
      'project',
      {
        kind: 'split',
        project_id: 'gig.mp4',
        revision: 2,
        source: 'external',
      },
      '',
    );
    expect(handle).toHaveBeenCalledWith({
      kind: 'split',
      project_id: 'gig.mp4',
      revision: 2,
      source: 'external',
    });
    stop();
    expect(close).toHaveBeenCalledTimes(1);
    expect(jobs.status).not.toBe('open');
    (options as EventsOptions | null)?.onEvent(
      'project',
      { project_id: 'gig.mp4', revision: 3 },
      '',
    );
    expect(handle).toHaveBeenCalledTimes(1); // unsubscribed
  });
});
