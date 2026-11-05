import { describe, expect, it } from 'vitest';
import type { ClipOut, ProjectOut, TrackOut } from '../api/client';
import { cutPoints, FRAME, neighbourCut } from './cuts';

const clip = (start: number, end: number) => ({ start, end }) as ClipOut;
const track = (...clips: ClipOut[]) => ({ clips }) as TrackOut;
const project = (duration: number, ...tracks: TrackOut[]) => ({ duration, tracks }) as ProjectOut;

describe('cutPoints', () => {
  it('is the clip edges of every track plus 0 and the end, sorted and without duplicates', () => {
    const p = project(
      30,
      track(clip(0, 10), clip(10, 30)),
      track(clip(4, 10), clip(12, 12.0000001)),
    );
    expect(cutPoints(p)).toEqual([0, 4, 10, 12, 30]);
  });
  it('is empty without a project', () => expect(cutPoints(null)).toEqual([]));
});

describe('neighbourCut', () => {
  const points = [0, 4, 10, 30];
  it('finds the next and the previous cut', () => {
    expect(neighbourCut(points, 5, 'next')).toBe(10);
    expect(neighbourCut(points, 5, 'prev')).toBe(4);
  });
  it('skips the cut it stands on, within a frame', () => {
    expect(neighbourCut(points, 10, 'next')).toBe(30);
    expect(neighbourCut(points, 10, 'prev')).toBe(4);
    expect(neighbourCut(points, 10 - FRAME / 2, 'next')).toBe(30);
    expect(neighbourCut(points, 10 - 2 * FRAME, 'next')).toBe(10);
  });
  it('stays at either end', () => {
    expect(neighbourCut(points, 30, 'next')).toBeNull();
    expect(neighbourCut(points, 0, 'prev')).toBeNull();
  });
});
