/**
 * Waveform columns from the server's stored curves (`GET /assets/{id}/waveform`): the browser never decodes audio. Two
 * curves come with the answer, peaks (min and max per bucket, 2000 buckets across the file) and loudness (RMS dB per
 * second); the finer one for this file is used, and each screen column takes the extreme of the samples it covers.
 */
import type { AssetWaveformOut } from '../api/client';

export interface Column {
  /** Both in -1..1; for the loudness curve symmetric around 0. */
  min: number;
  max: number;
}

/** dB shown as full height: -60 dB and below is drawn as a hairline. */
export const FLOOR_DB = -60;

/** Loudness in dB (-100..0) to a 0..1 height. */
export function dbToHeight(db: number): number {
  return Math.min(1, Math.max(0, (db - FLOOR_DB) / -FLOOR_DB));
}

/** Seconds per sample of each curve, and which one is finer. */
export function resolution(data: AssetWaveformOut): { peakSeconds: number; useLoudness: boolean } {
  const peakSeconds = data.peaks.length
    ? data.duration / data.peaks.length
    : Number.POSITIVE_INFINITY;
  const loud = data.loudness_window ?? Number.POSITIVE_INFINITY;
  return { peakSeconds, useLoudness: data.loudness.length > 0 && loud <= peakSeconds };
}

/**
 * The columns for source time `[from, to)` split into `count` equal parts. A column narrower than one sample repeats
 * the sample under it.
 */
export function waveformColumns(
  data: AssetWaveformOut,
  from: number,
  to: number,
  count: number,
): Column[] {
  const { peakSeconds, useLoudness } = resolution(data);
  const seconds = useLoudness ? (data.loudness_window as number) : peakSeconds;
  const length = useLoudness ? data.loudness.length : data.peaks.length;
  const columns: Column[] = [];
  if (length === 0 || count <= 0 || to <= from) return columns;
  const per = (to - from) / count;
  for (let c = 0; c < count; c++) {
    const a = (from + c * per) / seconds;
    const b = (from + (c + 1) * per) / seconds;
    const lo = Math.min(length - 1, Math.max(0, Math.floor(a)));
    const hi = Math.min(length - 1, Math.max(lo, Math.ceil(b) - 1));
    let min = 1;
    let max = -1;
    for (let i = lo; i <= hi; i++) {
      if (useLoudness) {
        const h = dbToHeight(data.loudness[i] as number);
        if (h > max) max = h;
        if (-h < min) min = -h;
      } else {
        const [pmin, pmax] = data.peaks[i] as [number, number];
        if (pmin < min) min = pmin;
        if (pmax > max) max = pmax;
      }
    }
    // Peaks are linear in amplitude, which draws quiet speech as a hairline: a square root keeps it readable.
    columns.push(
      useLoudness
        ? { min, max }
        : { min: -Math.sqrt(Math.max(0, -min)), max: Math.sqrt(Math.max(0, max)) },
    );
  }
  return columns;
}
