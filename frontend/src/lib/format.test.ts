import { describe, expect, it } from 'vitest';
import { baseName, formatClock, formatDuration, formatLength, parseClock } from './format';

describe('formatLength', () => {
  it('uses the spec wording', () => {
    expect(formatLength(9668)).toBe('2 h 41 min');
    expect(formatLength(252)).toBe('4 min 12 s');
    expect(formatLength(240)).toBe('4 min');
    expect(formatLength(38.4)).toBe('38 s');
    expect(formatLength(-5)).toBe('0 s');
  });
});

describe('formatClock', () => {
  it('is h:mm:ss', () => {
    expect(formatClock(0)).toBe('0:00:00');
    expect(formatClock(2472)).toBe('0:41:12');
    expect(formatClock(9668.9)).toBe('2:41:08');
  });
});

describe('formatDuration', () => {
  it('is m:ss, with hours only when there are some', () => {
    expect(formatDuration(12)).toBe('0:12');
    expect(formatDuration(200)).toBe('3:20');
    expect(formatDuration(9668.4)).toBe('2:41:08');
    expect(formatDuration(3600)).toBe('1:00:00');
    expect(formatDuration(-1)).toBe('0:00');
  });
});

describe('baseName', () => {
  it('keeps the file name of a workspace path', () => {
    expect(baseName('shows/2026/gig.mp4')).toBe('gig.mp4');
    expect(baseName('gig.mp4')).toBe('gig.mp4');
  });
});

describe('parseClock', () => {
  it('reads seconds, m:ss and h:mm:ss', () => {
    expect(parseClock('95')).toBe(95);
    expect(parseClock('1:35')).toBe(95);
    expect(parseClock(' 1:02:03 ')).toBe(3723);
    expect(parseClock('0:04:10.5')).toBe(250.5);
  });

  it('refuses what is not a time', () => {
    for (const bad of ['', 'abc', '1:75', '1:2:3:4', '-3', '1.5:30']) {
      expect(parseClock(bad), bad).toBeNull();
    }
  });
});
