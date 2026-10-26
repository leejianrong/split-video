// Region sizes (docs/ux-spec.md#layout): defaults, limits, clamping and persistence. Pure, so it tests without a
// DOM. The numbers mirror the `--size-*` tokens in styles/tokens.css (layout.test.ts fails if they drift).

export const TAB_IDS = [
  'media',
  'highlights',
  'transcript',
  'text',
  'captions',
  'audio',
  'effects',
] as const;
export type TabId = (typeof TAB_IDS)[number];

export type Resizable = 'panel' | 'inspector' | 'timeline';
export interface Sizes {
  panel: number;
  inspector: number;
  timeline: number;
}
export interface Viewport {
  width: number;
  height: number;
}

export const DEFAULT_SIZES: Readonly<Sizes> = { panel: 308, inspector: 340, timeline: 292 };
export const TOPBAR_HEIGHT = 48;
export const RAIL_WIDTH = 72;
export const PREVIEW_MIN = 480;
const LIMITS = {
  panel: { min: 260, max: 480 },
  inspector: { min: 280, max: 440 },
  timeline: { min: 200 },
} as const;
/** The timeline may grow to this share of the window height. */
export const TIMELINE_MAX_SHARE = 0.6;
/** Narrower than this, the editor shows a "window too small" notice instead of a broken layout. */
export const MIN_WINDOW = { width: 1280, height: 720 } as const;

const clamp = (value: number, min: number, max: number) =>
  Math.min(Math.max(value, min), Math.max(min, max));

/** Width left for the panel and the inspector together once the rail and the preview minimum are kept. */
const sideBudget = (viewport: Viewport) => viewport.width - RAIL_WIDTH - PREVIEW_MIN;

/** The limits of one region right now, given the others and the window. */
export function limitsOf(
  kind: Resizable,
  sizes: Sizes,
  viewport: Viewport,
): { min: number; max: number } {
  if (kind === 'timeline') {
    const min = LIMITS.timeline.min;
    return { min, max: Math.max(min, Math.floor(viewport.height * TIMELINE_MAX_SHARE)) };
  }
  const other = kind === 'panel' ? sizes.inspector : sizes.panel;
  const { min, max } = LIMITS[kind];
  return { min, max: Math.max(min, Math.min(max, sideBudget(viewport) - other)) };
}

/** The value a drag or key press may set: inside the region's limits, the preview never below its minimum. */
export function clampOne(kind: Resizable, value: number, sizes: Sizes, viewport: Viewport): number {
  const { min, max } = limitsOf(kind, sizes, viewport);
  return Math.round(clamp(value, min, max));
}

/**
 * Sizes that fit this window. When the window narrows, the left panel gives way first (down to its minimum), then
 * the inspector; the preview keeps at least 480 px (ux-spec narrowing order).
 */
export function clampSizes(sizes: Sizes, viewport: Viewport): Sizes {
  let panel = clamp(sizes.panel, LIMITS.panel.min, LIMITS.panel.max);
  let inspector = clamp(sizes.inspector, LIMITS.inspector.min, LIMITS.inspector.max);
  let over = panel + inspector - sideBudget(viewport);
  if (over > 0) {
    const fromPanel = Math.min(over, panel - LIMITS.panel.min);
    panel -= fromPanel;
    over -= fromPanel;
    inspector = Math.max(LIMITS.inspector.min, inspector - over);
  }
  const timeline = clamp(
    sizes.timeline,
    LIMITS.timeline.min,
    Math.max(LIMITS.timeline.min, Math.floor(viewport.height * TIMELINE_MAX_SHARE)),
  );
  return {
    panel: Math.round(panel),
    inspector: Math.round(inspector),
    timeline: Math.round(timeline),
  };
}

// ---- persistence: per browser, never in the project (ux-spec). localStorage can throw or be empty. ----

export const STORAGE_KEY = 'split-video.layout.v1';

export interface Stored {
  sizes: Sizes;
  tab: TabId;
}

export const DEFAULT_STORED: Readonly<Stored> = { sizes: DEFAULT_SIZES, tab: 'media' };

const isNumber = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

export type StoreLike = Pick<Storage, 'getItem' | 'setItem'>;

export function loadStored(storage: StoreLike | null = safeStorage()): Stored {
  try {
    const raw = storage?.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_STORED, sizes: { ...DEFAULT_SIZES } };
    const data: unknown = JSON.parse(raw);
    if (typeof data !== 'object' || data === null) throw new Error('not an object');
    const { sizes, tab } = data as { sizes?: Partial<Record<Resizable, unknown>>; tab?: unknown };
    const pick = (key: Resizable) => (isNumber(sizes?.[key]) ? sizes[key] : DEFAULT_SIZES[key]);
    return {
      sizes: { panel: pick('panel'), inspector: pick('inspector'), timeline: pick('timeline') },
      tab: TAB_IDS.includes(tab as TabId) ? (tab as TabId) : DEFAULT_STORED.tab,
    };
  } catch {
    return { ...DEFAULT_STORED, sizes: { ...DEFAULT_SIZES } };
  }
}

export function saveStored(stored: Stored, storage: StoreLike | null = safeStorage()): void {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(stored));
  } catch {
    // Private window, blocked site data or a full quota: the layout still works, it just is not remembered.
  }
}

function safeStorage(): StoreLike | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}
