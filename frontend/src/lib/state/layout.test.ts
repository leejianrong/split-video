import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  clampOne,
  clampSizes,
  DEFAULT_SIZES,
  limitsOf,
  loadStored,
  PREVIEW_MIN,
  RAIL_WIDTH,
  saveStored,
  STORAGE_KEY,
  TOPBAR_HEIGHT,
  type Sizes,
} from './layout';

const memory = (initial?: string) => {
  const data = new Map<string, string>();
  if (initial !== undefined) data.set(STORAGE_KEY, initial);
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    data,
  };
};

const W1440 = { width: 1440, height: 900 };

describe('tokens and defaults agree', () => {
  it('the TypeScript limits equal the --size-* tokens', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/lib/styles/tokens.css'), 'utf8');
    const token = (name: string) =>
      Number(new RegExp(`--size-${name}:\\s*(\\d+)px`).exec(css)?.[1]);
    expect(DEFAULT_SIZES.panel).toBe(token('panel-width'));
    expect(DEFAULT_SIZES.inspector).toBe(token('inspector-width'));
    expect(DEFAULT_SIZES.timeline).toBe(token('timeline-height'));
    expect(TOPBAR_HEIGHT).toBe(token('topbar-height'));
    expect(RAIL_WIDTH).toBe(token('tabrail-width'));
    expect(PREVIEW_MIN).toBe(token('preview-min'));
    expect(
      limitsOf('panel', { ...DEFAULT_SIZES, inspector: 280 }, { width: 3000, height: 900 }),
    ).toEqual({
      min: token('panel-min'),
      max: token('panel-max'),
    });
    expect(
      limitsOf('inspector', { ...DEFAULT_SIZES, panel: 260 }, { width: 3000, height: 900 }),
    ).toEqual({
      min: token('inspector-min'),
      max: token('inspector-max'),
    });
    expect(limitsOf('timeline', DEFAULT_SIZES, W1440).min).toBe(token('timeline-min'));
  });
});

describe('clampSizes', () => {
  it('leaves the wireframe sizes alone at 1440 and gives the preview 720', () => {
    const sizes = clampSizes(DEFAULT_SIZES, W1440);
    expect(sizes).toEqual(DEFAULT_SIZES);
    expect(1440 - RAIL_WIDTH - sizes.panel - sizes.inspector).toBe(720);
  });

  it('keeps every region inside its limits', () => {
    const wild: Sizes = { panel: 9999, inspector: 1, timeline: 99999 };
    const sizes = clampSizes(wild, { width: 3000, height: 1000 });
    expect(sizes).toEqual({ panel: 480, inspector: 280, timeline: 600 });
    expect(clampSizes({ panel: 0, inspector: 0, timeline: 0 }, W1440)).toEqual({
      panel: 260,
      inspector: 280,
      timeline: 200,
    });
  });

  it('narrows the panel first, then the inspector, and never takes the preview under 480', () => {
    const wide: Sizes = { panel: 480, inspector: 440, timeline: 292 };
    for (const width of [1280, 1366, 1440, 1920]) {
      const s = clampSizes(wide, { width, height: 720 });
      expect(width - RAIL_WIDTH - s.panel - s.inspector).toBeGreaterThanOrEqual(PREVIEW_MIN);
    }
    const s1280 = clampSizes(wide, { width: 1280, height: 720 });
    // 1280 - 72 - 480 = 728 left for both: the panel gives way first.
    expect(s1280).toEqual({ panel: 288, inspector: 440, timeline: 292 });
    // With a 280 inspector the budget is 728: 480 + 280 = 760 over by 32, so the panel gives 32.
    expect(
      clampSizes({ panel: 480, inspector: 280, timeline: 292 }, { width: 1280, height: 720 }),
    ).toEqual({
      panel: 448,
      inspector: 280,
      timeline: 292,
    });
  });

  it('caps the timeline at 60 percent of the window height, but never below 200', () => {
    expect(
      clampSizes({ ...DEFAULT_SIZES, timeline: 500 }, { width: 1440, height: 720 }).timeline,
    ).toBe(432);
    expect(
      clampSizes({ ...DEFAULT_SIZES, timeline: 500 }, { width: 1440, height: 300 }).timeline,
    ).toBe(200);
  });
});

describe('clampOne', () => {
  it('stops a drag at the limits and at the preview minimum', () => {
    const win = { width: 1280, height: 720 };
    const sizes = clampSizes(DEFAULT_SIZES, win);
    expect(clampOne('panel', 2000, sizes, win)).toBe(728 - sizes.inspector);
    expect(clampOne('panel', -50, sizes, win)).toBe(260);
    expect(clampOne('inspector', 2000, sizes, win)).toBe(Math.min(440, 728 - sizes.panel));
    expect(clampOne('timeline', 2000, sizes, win)).toBe(432);
    expect(clampOne('timeline', 10, sizes, win)).toBe(200);
  });
});

describe('persistence', () => {
  it('round-trips sizes and the tab', () => {
    const store = memory();
    saveStored({ sizes: { panel: 300, inspector: 320, timeline: 250 }, tab: 'captions' }, store);
    expect(loadStored(store)).toEqual({
      sizes: { panel: 300, inspector: 320, timeline: 250 },
      tab: 'captions',
    });
  });

  it('falls back to the defaults on nothing, junk or a wrong shape', () => {
    for (const raw of [
      undefined,
      'not json',
      'null',
      '[]',
      '{"sizes":{"panel":"wide"},"tab":"nope"}',
    ]) {
      const loaded = loadStored(memory(raw));
      expect(loaded.sizes).toEqual(DEFAULT_SIZES);
      expect(loaded.tab).toBe('media');
    }
  });

  it('keeps the fields that are valid', () => {
    const loaded = loadStored(memory('{"sizes":{"panel":333,"inspector":"x"},"tab":"audio"}'));
    expect(loaded).toEqual({ sizes: { ...DEFAULT_SIZES, panel: 333 }, tab: 'audio' });
  });

  it('survives storage that throws', () => {
    const broken = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('quota');
      },
    };
    expect(loadStored(broken).sizes).toEqual(DEFAULT_SIZES);
    expect(() => saveStored({ sizes: DEFAULT_SIZES, tab: 'media' }, broken)).not.toThrow();
    expect(loadStored(null).tab).toBe('media');
  });
});
