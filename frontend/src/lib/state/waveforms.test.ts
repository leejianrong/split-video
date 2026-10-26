import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getWave = vi.fn();
const startWave = vi.fn();
vi.mock('../api/generated/sdk.gen', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/generated/sdk.gen')>()),
  getWaveformApiV1AssetsAssetIdWaveformGet: (...args: unknown[]) => getWave(...args),
  startWaveformApiV1AssetsAssetIdWaveformPost: (...args: unknown[]) => startWave(...args),
}));

import { ApiError } from '../api/client';
import { WaveformStore } from './waveforms.svelte';

const data = {
  asset: 'a_1',
  duration: 10,
  peaks: [[-0.5, 0.5]],
  loudness_window: 1,
  loudness: [-20],
};
const notYet = () =>
  new ApiError({
    code: 'unknown',
    message: 'no waveform yet: POST this path to make one',
    status: 404,
  });
const noAudio = () =>
  new ApiError({
    code: 'unknown',
    message: 'this asset has no audio, so it has no waveform',
    status: 404,
  });

beforeEach(() => {
  vi.useFakeTimers();
  getWave.mockReset();
  startWave.mockReset();
});
afterEach(() => vi.useRealTimers());

describe('WaveformStore', () => {
  it('uses the stored curves when the server has them, without starting a job', async () => {
    getWave.mockResolvedValue({ data });
    const store = new WaveformStore();
    store.ensure('a_1');
    expect(store.get('a_1')?.status).toBe('loading');
    await vi.waitFor(() => expect(store.get('a_1')?.status).toBe('ready'));
    expect(store.get('a_1')?.data).toEqual(data);
    expect(getWave).toHaveBeenCalledWith({ path: { asset_id: 'a_1' } });
    expect(startWave).not.toHaveBeenCalled();
    store.dispose();
  });

  it('on a 404 starts the job once, then polls until the curves are there', async () => {
    getWave
      .mockRejectedValueOnce(notYet())
      .mockRejectedValueOnce(notYet())
      .mockResolvedValue({ data });
    startWave.mockResolvedValue({ data: { job_id: 'j' } });
    const store = new WaveformStore();
    store.ensure('a_1');
    store.ensure('a_1'); // asking again is free
    await vi.waitFor(() => expect(store.get('a_1')?.status).toBe('computing'));
    expect(startWave).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(600);
    expect(store.get('a_1')?.status).toBe('computing');
    await vi.advanceTimersByTimeAsync(2100);
    expect(store.get('a_1')?.status).toBe('ready');
    expect(startWave).toHaveBeenCalledTimes(1);
    store.dispose();
  });

  it('an asset without audio is none, whichever call says so', async () => {
    getWave.mockRejectedValue(noAudio());
    const store = new WaveformStore();
    store.ensure('a_mute');
    await vi.waitFor(() => expect(store.get('a_mute')?.status).toBe('none'));
    getWave.mockRejectedValue(notYet());
    startWave.mockRejectedValue(
      new ApiError({ code: 'unknown', message: 'no audio', status: 422 }),
    );
    store.ensure('a_other');
    await vi.waitFor(() => expect(store.get('a_other')?.status).toBe('none'));
    store.dispose();
  });

  it('a server error is failed, and reset forgets everything and stops polling', async () => {
    getWave.mockRejectedValue(new ApiError({ code: 'unknown', message: 'boom', status: 500 }));
    const store = new WaveformStore();
    store.ensure('a_1');
    await vi.waitFor(() => expect(store.get('a_1')?.status).toBe('failed'));
    getWave.mockRejectedValue(notYet());
    startWave.mockResolvedValue({ data: { job_id: 'j' } });
    store.ensure('a_2');
    await vi.waitFor(() => expect(store.get('a_2')?.status).toBe('computing'));
    store.reset();
    expect(store.get('a_2')).toBeUndefined();
    getWave.mockClear();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(getWave).not.toHaveBeenCalled();
    store.dispose();
  });
});
