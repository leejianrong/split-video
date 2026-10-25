import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { contrastRatio } from './contrast';

// vitest runs from frontend/, so the path is relative to it.
const tokensCss = readFileSync(resolve(process.cwd(), 'src/lib/styles/tokens.css'), 'utf8');

function tokens(): Record<string, string> {
  return Object.fromEntries(
    [...tokensCss.matchAll(/(--[\w-]+):\s*(#[0-9a-f]{6});/gi)].map((m) => [m[1]!, m[2]!]),
  );
}

describe('contrastRatio', () => {
  it('matches the WCAG reference values', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5);
    expect(contrastRatio('#ffffff', '#ffffff')).toBeCloseTo(1, 5);
  });
});

describe('token pairs (ux-spec accessibility: AA)', () => {
  const t = tokens();
  it.each([
    ['--color-text', '--color-bg', 4.5],
    ['--color-text', '--color-surface', 4.5],
    ['--color-text-muted', '--color-surface', 4.5],
    ['--color-accent-text', '--color-accent', 4.5],
    ['--color-accent', '--color-bg', 3],
  ])('%s on %s is at least %s:1', (fg, bg, min) => {
    expect(contrastRatio(t[fg]!, t[bg]!)).toBeGreaterThanOrEqual(min);
  });
});
