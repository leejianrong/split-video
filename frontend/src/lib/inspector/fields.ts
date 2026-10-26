/** Pure helpers of the contextual inspector: parsing, validation and the maths behind the presets. No DOM, no store. */

export interface NumberRule {
  /** What the field is called in the hint ("Volume"). */
  name: string;
  min: number;
  max: number;
  unit?: string;
}

export type Parsed = { ok: true; value: number } | { ok: false; error: string };

/** Parse what was typed ("12", "-3.5", "12,5", "50 %") and check it against the rule, with a "Fix:" hint (ux-spec principle 6). */
export function parseNumber(text: string, rule: NumberRule): Parsed {
  const cleaned = text
    .trim()
    .replace(',', '.')
    .replace(/\s*(%|db|x)$/i, '');
  const unit = rule.unit ? ` ${rule.unit}` : '';
  const range = `${format(rule.min)} to ${format(rule.max)}${unit}`;
  if (cleaned === '' || !/^[+-]?(\d+\.?\d*|\.\d+)$/.test(cleaned)) {
    return { ok: false, error: `${rule.name} must be a number. Fix: type a number from ${range}.` };
  }
  const value = Number(cleaned);
  if (value < rule.min || value > rule.max) {
    return {
      ok: false,
      error: `${rule.name} must be from ${range}. Fix: type a value in that range.`,
    };
  }
  return { ok: true, value };
}

/** A number for display: at most 2 decimals, no trailing zeros, no negative zero. */
export function format(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  return String(Object.is(rounded, -0) ? 0 : rounded);
}

/** The engine's gain is linear 0 to 4; the inspector speaks decibels (ux-spec: "Volume (dB)"). */
export const VOLUME_DB = { min: -40, max: 12 } as const;

export function gainToDb(gain: number): number {
  if (gain <= 0) return VOLUME_DB.min;
  return Math.max(
    VOLUME_DB.min,
    Math.min(VOLUME_DB.max, Math.round(20 * Math.log10(gain) * 10) / 10),
  );
}

export function dbToGain(db: number): number {
  // 12 dB is 3.98, inside the engine's 0..4; the bottom of the range is silence.
  if (db <= VOLUME_DB.min) return 0;
  return Math.round(10 ** (db / 20) * 10000) / 10000;
}

export interface Crop {
  x: number;
  y: number;
  w: number;
  h: number;
}

export const CROP_MIN = 0.02;

export type CropPreset = '9:16' | '1:1' | '16:9';

const RATIOS: Record<CropPreset, number> = { '9:16': 9 / 16, '1:1': 1, '16:9': 16 / 9 };

/**
 * The largest centred crop of a `width` x `height` source frame with the preset's shape, as fractions of the frame
 * (the engine's crop). `null` when the source's size is not known or the preset is the source's own shape.
 */
export function presetCrop(
  preset: CropPreset,
  width: number | null,
  height: number | null,
): Crop | null {
  if (!width || !height) return null;
  const source = width / height;
  const target = RATIOS[preset];
  let w = 1;
  let h = 1;
  if (target < source) w = target / source;
  else h = source / target;
  if (Math.abs(w - 1) < 1e-3 && Math.abs(h - 1) < 1e-3) return null;
  const round = (n: number) => Math.round(n * 10000) / 10000;
  return { x: round((1 - w) / 2), y: round((1 - h) / 2), w: round(w), h: round(h) };
}

/** Which preset (if any) the crop is, for the pressed state of its button. */
export function matchPreset(
  crop: Crop | null,
  width: number | null,
  height: number | null,
): CropPreset | null {
  if (!crop) return null;
  for (const preset of Object.keys(RATIOS) as CropPreset[]) {
    const want = presetCrop(preset, width, height);
    if (
      want &&
      Math.abs(want.x - crop.x) < 2e-3 &&
      Math.abs(want.y - crop.y) < 2e-3 &&
      Math.abs(want.w - crop.w) < 2e-3 &&
      Math.abs(want.h - crop.h) < 2e-3
    ) {
      return preset;
    }
  }
  return null;
}

/** The crop with one field changed (percent in, fractions out), or an error when it would leave the frame. */
export function withCropField(
  crop: Crop | null,
  field: keyof Crop,
  percent: number,
): { ok: true; crop: Crop } | { ok: false; error: string } {
  const base = crop ?? { x: 0, y: 0, w: 1, h: 1 };
  const next = { ...base, [field]: Math.round(percent * 100) / 10000 };
  if (next.w < CROP_MIN || next.h < CROP_MIN) {
    return {
      ok: false,
      error: `The region must be at least ${CROP_MIN * 100}% wide and high. Fix: make it larger.`,
    };
  }
  if (next.x + next.w > 1.0001 || next.y + next.h > 1.0001) {
    return {
      ok: false,
      error: 'The region leaves the frame. Fix: lower X or Y, or make it smaller.',
    };
  }
  return { ok: true, crop: next };
}

/** Defaults for "Reset": every property the video, audio and crop tabs change. */
export const CLIP_DEFAULTS = {
  gain: 1,
  muted: false,
  x: 0.5,
  y: 0.5,
  scale: 1,
  opacity: 1,
  brightness: 0,
  contrast: 1,
  saturation: 1,
  crop: null,
} as const;

export type ClipProperties = Partial<{
  gain: number;
  muted: boolean;
  x: number;
  y: number;
  scale: number;
  opacity: number;
  brightness: number;
  contrast: number;
  saturation: number;
  crop: Crop | null;
}>;

/** One `set_clip_properties` op per clip (docs/edit-ops.md); the engine applies the batch atomically. */
export function propertyOps(
  clipIds: readonly string[],
  properties: ClipProperties,
): Record<string, unknown>[] {
  return clipIds.map((clip) => ({ op: 'set_clip_properties', clip, ...properties }));
}
