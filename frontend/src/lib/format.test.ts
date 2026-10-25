import { describe, expect, it } from 'vitest';
import { formatClock, formatLength } from './format';

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
