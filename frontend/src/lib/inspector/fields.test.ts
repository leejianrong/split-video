import { describe, expect, it } from 'vitest';
import {
  CLIP_DEFAULTS,
  dbToGain,
  gainToDb,
  matchPreset,
  parseNumber,
  presetCrop,
  propertyOps,
  withCropField,
} from './fields';

const volume = { name: 'Volume', min: -40, max: 12, unit: 'dB' };

describe('parseNumber', () => {
  it('accepts numbers, commas and units', () => {
    expect(parseNumber('-3.5', volume)).toEqual({ ok: true, value: -3.5 });
    expect(parseNumber(' 6 dB ', volume)).toEqual({ ok: true, value: 6 });
    expect(parseNumber('2,5', volume)).toEqual({ ok: true, value: 2.5 });
  });
  it('explains a non-number and an out-of-range value with a Fix', () => {
    const bad = parseNumber('loud', volume);
    expect(bad.ok).toBe(false);
    if (!bad.ok) expect(bad.error).toContain('Fix:');
    const high = parseNumber('40', volume);
    expect(high.ok).toBe(false);
    if (!high.ok) expect(high.error).toMatch(/-40 to 12 dB.*Fix:/);
    expect(parseNumber('', volume).ok).toBe(false);
  });
});

describe('decibels', () => {
  it('round-trips and stays inside the engine range', () => {
    expect(gainToDb(1)).toBe(0);
    expect(gainToDb(2)).toBe(6);
    expect(dbToGain(0)).toBe(1);
    expect(dbToGain(12)).toBeLessThanOrEqual(4);
    expect(dbToGain(-40)).toBe(0);
    expect(gainToDb(0)).toBe(-40);
  });
});

describe('presetCrop', () => {
  it('centres a 9:16 crop in a 16:9 frame', () => {
    const crop = presetCrop('9:16', 1920, 1080)!;
    expect(crop.h).toBe(1);
    expect(crop.w).toBeCloseTo(0.3164, 3);
    expect(crop.x + crop.w / 2).toBeCloseTo(0.5, 3);
  });
  it('makes a square and has nothing to do for the source shape', () => {
    expect(presetCrop('1:1', 1920, 1080)!.w).toBeCloseTo(0.5625, 3);
    expect(presetCrop('16:9', 1920, 1080)).toBeNull();
    expect(presetCrop('1:1', null, null)).toBeNull();
  });
  it('crops height for a tall source', () => {
    const crop = presetCrop('16:9', 1080, 1920)!;
    expect(crop.w).toBe(1);
    expect(crop.h).toBeCloseTo(0.3164, 3);
  });
  it('recognises its own output', () => {
    expect(matchPreset(presetCrop('1:1', 1920, 1080), 1920, 1080)).toBe('1:1');
    expect(matchPreset({ x: 0.1, y: 0.1, w: 0.5, h: 0.5 }, 1920, 1080)).toBeNull();
    expect(matchPreset(null, 1920, 1080)).toBeNull();
  });
});

describe('withCropField', () => {
  it('turns percent into fractions and refuses a region outside the frame', () => {
    const ok = withCropField({ x: 0, y: 0, w: 0.5, h: 1 }, 'x', 25);
    expect(ok).toEqual({ ok: true, crop: { x: 0.25, y: 0, w: 0.5, h: 1 } });
    const out = withCropField({ x: 0, y: 0, w: 0.5, h: 1 }, 'x', 60);
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.error).toContain('Fix:');
    expect(withCropField(null, 'w', 1).ok).toBe(false);
  });
});

describe('propertyOps', () => {
  it('makes one set_clip_properties op per clip', () => {
    expect(propertyOps(['a', 'b'], { gain: 2 })).toEqual([
      { op: 'set_clip_properties', clip: 'a', gain: 2 },
      { op: 'set_clip_properties', clip: 'b', gain: 2 },
    ]);
  });
  it('defaults reset every property', () => {
    expect(Object.keys(CLIP_DEFAULTS)).toContain('crop');
  });
});
