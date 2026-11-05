import type { ProjectOut } from '../api/client';

/**
 * One frame of the project, in seconds. Every canvas preset renders at 30 fps (src/split_video/presets.py), and the
 * server snaps stills to that grid, so this is the frame step and the margin that skips the boundary being stood on.
 */
export const FRAME = 1 / 30;
/** What J and L step, in seconds (ux-spec #shortcuts). */
export const STEP_SECONDS = 5;

/** Where a cut can be: 0, the timeline end, and the start and end of every clip on every track, sorted and deduplicated. */
export function cutPoints(timeline: ProjectOut | null): number[] {
  if (!timeline) return [];
  const all = [
    0,
    timeline.duration,
    ...timeline.tracks.flatMap((t) => t.clips.flatMap((c) => [c.start, c.end])),
  ];
  return all.sort((a, b) => a - b).filter((t, i, a) => i === 0 || t - a[i - 1]! > 1e-6);
}

/** The nearest cut a frame or more away from `time` in `direction`, or null when there is none (already at the edge). */
export function neighbourCut(
  points: readonly number[],
  time: number,
  direction: 'prev' | 'next',
): number | null {
  return direction === 'next'
    ? (points.find((p) => p > time + FRAME - 1e-6) ?? null)
    : (points.findLast((p) => p < time - FRAME + 1e-6) ?? null);
}
