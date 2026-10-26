/**
 * Filmstrip thumbnails: which frames a clip needs for the part of it that is on screen, and a loader that fetches them
 * a few at a time. The frames come from `GET /assets/{id}/frame` (one ffmpeg seek each), so the loader is gentle:
 * at most `MAX_PARALLEL` requests, only what is currently wanted, never the same URL twice.
 */

/** Spacings (seconds of source) a thumbnail may stand for: tiles of one zoom level share a grid, so scrolling reuses them. */
const GRID = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];

/** The smallest grid step that is at least `seconds`. */
export function gridStep(seconds: number): number {
  return GRID.find((g) => g >= seconds) ?? (GRID[GRID.length - 1] as number);
}

export interface Tile {
  /** The grid index: the tile stands for source times `[index * step, (index + 1) * step)`. */
  index: number;
  /** Left edge in px inside the clip (may be negative for the first tile), and its width. */
  left: number;
  width: number;
  /** The source time of the frame to show. */
  time: number;
}

/**
 * The tiles of a clip's filmstrip for the visible part of it. `clipIn` / `clipOut` are source seconds, `visibleFrom` /
 * `visibleTo` the on-screen part in the same seconds. A tile is at least `tileWidth` px wide; when zoomed out so far
 * that the clip is narrower than a tile, there is none.
 */
export function filmstripTiles(options: {
  clipIn: number;
  clipOut: number;
  pxPerSecond: number;
  tileWidth: number;
  visibleFrom: number;
  visibleTo: number;
}): Tile[] {
  const { clipIn, clipOut, pxPerSecond, tileWidth } = options;
  if ((clipOut - clipIn) * pxPerSecond < tileWidth * 0.75) return [];
  const step = gridStep(tileWidth / pxPerSecond);
  const from = Math.max(clipIn, options.visibleFrom);
  const to = Math.min(clipOut, options.visibleTo);
  if (to <= from) return [];
  const first = Math.floor(from / step);
  const last = Math.ceil(to / step) - 1;
  const tiles: Tile[] = [];
  for (let index = first; index <= last; index++) {
    const mid = (index + 0.5) * step;
    tiles.push({
      index,
      left: (index * step - clipIn) * pxPerSecond,
      width: step * pxPerSecond,
      // The frame in the middle of the tile, kept inside the clip so a trimmed clip never shows what was cut away.
      time: Math.round(Math.min(Math.max(mid, clipIn), clipOut - 0.05) * 100) / 100,
    });
  }
  return tiles;
}

export function frameUrl(assetId: string, time: number, width = 176): string {
  return `/api/v1/assets/${encodeURIComponent(assetId)}/frame?time=${time}&width=${width}`;
}

export const MAX_PARALLEL = 4;
const MAX_REMEMBERED = 800;

/** What the loader needs from the environment (an `Image`), so tests need no browser. */
export interface ImageLike {
  onload: (() => void) | null;
  onerror: (() => void) | null;
  src: string;
}

/**
 * Fetches thumbnails by URL, a few at a time. `request` replaces the wanted list (scrolling away drops what is no
 * longer needed from the queue; requests already running finish, since the browser caches them); `isLoaded` says
 * whether a URL can be shown. `onChange` fires when one more is ready.
 */
export class ThumbLoader {
  #loaded = new Set<string>();
  #failed = new Set<string>();
  #running = new Set<string>();
  #queue: string[] = [];
  readonly #create: () => ImageLike;
  readonly #max: number;
  /** Called when a thumbnail finished loading. */
  onChange: () => void = () => {};

  constructor(
    create: () => ImageLike = () => new Image() as unknown as ImageLike,
    max = MAX_PARALLEL,
  ) {
    this.#create = create;
    this.#max = max;
  }

  get running(): number {
    return this.#running.size;
  }

  get queued(): number {
    return this.#queue.length;
  }

  isLoaded(url: string): boolean {
    return this.#loaded.has(url);
  }

  /** Make `urls` (most important first) the wanted list. */
  request(urls: readonly string[]): void {
    this.#queue = urls.filter(
      (u) => !this.#loaded.has(u) && !this.#failed.has(u) && !this.#running.has(u),
    );
    this.#pump();
  }

  #pump(): void {
    while (this.#running.size < this.#max) {
      const url = this.#queue.shift();
      if (url === undefined) return;
      this.#running.add(url);
      const image = this.#create();
      const done = (ok: boolean) => {
        image.onload = image.onerror = null;
        this.#running.delete(url);
        if (ok) {
          this.#loaded.add(url);
          if (this.#loaded.size > MAX_REMEMBERED) {
            const oldest = this.#loaded.values().next().value;
            if (oldest !== undefined) this.#loaded.delete(oldest);
          }
        } else this.#failed.add(url);
        this.onChange();
        this.#pump();
      };
      image.onload = () => done(true);
      image.onerror = () => done(false);
      image.src = url;
    }
  }
}
