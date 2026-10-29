import { describe, expect, it, vi } from 'vitest';

const connect = vi.fn();
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  connectEvents: (...args: unknown[]) => connect(...args),
}));

import type { EventsOptions } from '../api/client';
import { connectLive } from './live';
import { jobs } from './jobs.svelte';
import { clipSelection } from './clipSelection.svelte';
import { project } from './project.svelte';
import { toasts } from './toasts.svelte';

describe('connectLive', () => {
  it('opens the one event stream, routes project events to the project, and closes with it', () => {
    const close = vi.fn();
    let options: EventsOptions | null = null;
    connect.mockImplementation((o: EventsOptions) => {
      options = o;
      return close;
    });
    const handle = vi.spyOn(project, 'handleProjectEvent').mockResolvedValue(false);
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

  it('says what changed and drops selected clips that are gone after a reload', async () => {
    connect.mockImplementation(() => vi.fn());
    const handle = vi.spyOn(project, 'handleProjectEvent').mockImplementation(async () => {
      project.timeline = {
        revision: 5,
        tracks: [{ id: 't', clips: [{ id: 'c1' }] }],
      } as unknown as typeof project.timeline;
      return true;
    });
    clipSelection.ids = ['c1', 'c2'];
    toasts.clear();
    const stop = connectLive();
    const options = connect.mock.calls.at(-1)?.[0] as EventsOptions;
    options.onEvent(
      'project',
      { kind: 'timeline', project_id: 'g', revision: 5, source: 'external' },
      '',
    );
    await vi.waitFor(() => expect(toasts.items).toHaveLength(1));
    expect(clipSelection.ids).toEqual(['c1']);
    expect(toasts.items[0]?.message).toMatch(
      /changed by another tool.*clip you selected was removed/,
    );
    handle.mockResolvedValue(false);
    options.onEvent(
      'project',
      { kind: 'timeline', project_id: 'g', revision: 5, source: 'local' },
      '',
    );
    await Promise.resolve();
    expect(toasts.items).toHaveLength(1);
    stop();
  });
});
