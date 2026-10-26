import { describe, expect, it } from 'vitest';
import type { AssetWaveformOut } from '../api/client';
import { dbToHeight, resolution, waveformColumns } from './waveform';

const data = (extra: Partial<AssetWaveformOut>): AssetWaveformOut => ({
  asset: 'a_1',
  duration: 100,
  peaks: [],
  loudness_window: 1,
  loudness: [],
  ...extra,
});

describe('waveform columns', () => {
  it('uses the finer curve: peaks for a short file, loudness for a long one', () => {
    const peaks = Array.from({ length: 2000 }, () => [-0.5, 0.5] as [number, number]);
    expect(resolution(data({ duration: 100, peaks, loudness: [-20] })).useLoudness).toBe(false); // 0.05 s vs 1 s
    expect(resolution(data({ duration: 10_800, peaks, loudness: [-20] })).useLoudness).toBe(true); // 5.4 s vs 1 s
    expect(resolution(data({ duration: 10, peaks: [], loudness: [-20] })).useLoudness).toBe(true);
  });

  it('each column takes the extremes of the peaks it covers', () => {
    const peaks: [number, number][] = [
      [-0.1, 0.2],
      [-0.9, 0.3],
      [-0.2, 0.8],
      [-0.4, 0.4],
    ];
    const cols = waveformColumns(data({ duration: 4, peaks, loudness: [] }), 0, 4, 2);
    // the extremes of each pair of buckets, square-rooted so quiet audio is still visible
    expect(cols[0]?.min).toBeCloseTo(-Math.sqrt(0.9), 6);
    expect(cols[0]?.max).toBeCloseTo(Math.sqrt(0.3), 6);
    expect(cols[1]?.min).toBeCloseTo(-Math.sqrt(0.4), 6);
    expect(cols[1]?.max).toBeCloseTo(Math.sqrt(0.8), 6);
  });

  it('loudness is drawn symmetric around zero, -60 dB and below flat, 0 dB full height', () => {
    expect(dbToHeight(0)).toBe(1);
    expect(dbToHeight(-30)).toBeCloseTo(0.5, 6);
    expect(dbToHeight(-100)).toBe(0);
    const cols = waveformColumns(
      data({ duration: 3, peaks: [], loudness: [0, -30, -100] }),
      0,
      3,
      3,
    );
    expect(cols.map((c) => [c.min, c.max])).toEqual([
      [-1, 1],
      [-0.5, 0.5],
      [-0, 0],
    ]);
  });

  it('a column narrower than a sample repeats it, and a window past the end clamps', () => {
    const cols = waveformColumns(data({ duration: 2, peaks: [], loudness: [0, -60] }), 0, 1, 8);
    expect(cols).toHaveLength(8);
    expect(cols.every((c) => c.max === 1)).toBe(true);
    expect(waveformColumns(data({ duration: 2, loudness: [0, -60] }), 5, 6, 2)).toHaveLength(2);
    expect(waveformColumns(data({ duration: 2 }), 0, 1, 4)).toEqual([]);
  });
});
