import { readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
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

// Every text / surface and control / surface pair a component uses. Add the pair here when a component uses a
// new one. Text: 4.5:1. Controls, borders that identify a control, icons and focus rings: 3:1 (ux-spec #accessibility).
const TEXT_PAIRS: [string, string][] = [
  ...[
    '--color-bg',
    '--color-surface',
    '--color-surface-raised',
    '--color-surface-hover',
    '--color-surface-active',
  ].flatMap((bg): [string, string][] => [['--color-text', bg]]),
  ...['--color-bg', '--color-surface', '--color-surface-raised', '--color-surface-hover'].flatMap(
    (bg): [string, string][] => [['--color-text-muted', bg]],
  ),
  ['--color-accent-text', '--color-accent'],
  ['--color-accent-text', '--color-accent-hover'],
  ['--color-accent-text', '--color-accent-active'],
  ['--color-text', '--color-accent-subtle'],
  ['--color-danger-text', '--color-danger-solid'],
  ['--color-danger-text', '--color-danger-hover'],
  ['--color-danger', '--color-surface'],
  ['--color-danger', '--color-surface-raised'],
  ['--color-danger', '--color-bg'],
  ['--color-warn', '--color-surface-raised'],
  ['--color-ok', '--color-surface-raised'],
  ['--color-ok', '--color-surface'],
  ['--color-accent', '--color-surface'],
  ['--color-accent', '--color-surface-raised'],
  // Timeline clip labels
  ['--color-text', '--color-clip-segment'],
  ['--color-text', '--color-clip-song'],
  ['--color-text', '--color-clip-overlay'],
  ['--color-text', '--color-clip-music'],
  ['--color-clip-text-ink', '--color-clip-text'],
];

const CONTROL_PAIRS: [string, string][] = [
  ['--color-accent', '--color-bg'],
  ['--color-accent', '--color-surface'],
  ['--color-accent', '--color-surface-raised'],
  ['--color-focus', '--color-bg'],
  ['--color-focus', '--color-surface'],
  ['--color-focus', '--color-surface-raised'],
  ['--color-focus', '--color-surface-hover'],
  ['--color-border-strong', '--color-bg'],
  ['--color-border-strong', '--color-surface'],
  ['--color-border-strong', '--color-surface-raised'],
  ['--color-danger-solid', '--color-surface'],
  ['--color-danger', '--color-bg'],
  ['--color-warn', '--color-surface-raised'],
  ['--color-ok', '--color-surface-raised'],
  // Timeline: the playhead and the waveform against their lanes, a clip's selection outline against its colour
  ['--color-playhead', '--color-surface'],
  ['--color-playhead', '--color-bg'],
  ['--color-waveform', '--color-surface'],
  ['--color-waveform', '--color-bg'],
  ['--color-text', '--color-clip-segment'],
  ['--color-text', '--color-clip-song'],
];

describe('token pairs (ux-spec accessibility: AA)', () => {
  const t = tokens();

  it.each(TEXT_PAIRS)('text %s on %s is at least 4.5:1', (fg, bg) => {
    expect(t[fg], fg).toBeDefined();
    expect(t[bg], bg).toBeDefined();
    expect(contrastRatio(t[fg]!, t[bg]!)).toBeGreaterThanOrEqual(4.5);
  });

  it.each(CONTROL_PAIRS)('control %s on %s is at least 3:1', (fg, bg) => {
    expect(t[fg], fg).toBeDefined();
    expect(t[bg], bg).toBeDefined();
    expect(contrastRatio(t[fg]!, t[bg]!)).toBeGreaterThanOrEqual(3);
  });
});

describe('components use tokens only', () => {
  const files = (dir: string): string[] =>
    readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
      const full = join(dir, e.name);
      if (e.isDirectory()) return e.name === 'generated' ? [] : files(full);
      return /\.(svelte|css)$/.test(e.name) ? [full] : [];
    });
  const sources = files(resolve(process.cwd(), 'src')).filter((f) => !f.endsWith('tokens.css'));

  it('finds the component files', () => {
    expect(sources.length).toBeGreaterThan(10);
  });

  it.each(sources.map((f) => [f.replace(process.cwd() + '/', ''), f]))(
    '%s has no hard-coded colour',
    (_name, file) => {
      const text = readFileSync(file!, 'utf8');
      expect(text.match(/#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/gi) ?? []).toEqual([]);
    },
  );
});
