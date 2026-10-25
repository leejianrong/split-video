import { describe, expect, it } from 'vitest';
import {
  MAX_PARALLEL,
  ThumbLoader,
  filmstripTiles,
  frameUrl,
  gridStep,
  type ImageLike,
} from './thumbs';

describe('filmstripTiles', () => {
  const base = { clipIn: 0, clipOut: 600, tileWidth: 85, visibleFrom: 0, visibleTo: 600 };

  it('shares a grid across zoom levels and lays tiles at their source time', () => {
    expect(gridStep(0.4)).toBe(0.5);
    expect(gridStep(8.5)).toBe(10);
    expect(gridStep(1e9)).toBe(3600);
    const tiles = filmstripTiles({ ...base, pxPerSecond: 8.5, visibleTo: 60 });
    expect(tiles.map((t) => t.index)).toEqual([0, 1, 2, 3, 4, 5]); // 10 s tiles
    expect(tiles[1]).toMatchObject({ left: 85, width: 85, time: 15 });
  });

  it('only covers the visible part of the clip, so a long clip costs the same as a short one', () => {
    for (const clipOut of [4000, 10_000]) {
      const tiles = filmstripTiles({
        ...base,
        clipOut,
        pxPerSecond: 8.5,
        visibleFrom: 3000,
        visibleTo: 3000 + 1000 / 8.5,
      });
      expect(tiles.length).toBeLessThan(16);
      expect(tiles.length).toBeGreaterThan(8);
    }
    expect(
      filmstripTiles({ ...base, clipOut: 600, pxPerSecond: 8.5, visibleFrom: 900, visibleTo: 990 }),
    ).toEqual([]);
  });

  it('a trimmed clip never shows frames from the part that was cut away', () => {
    const tiles = filmstripTiles({
      ...base,
      clipIn: 33,
      clipOut: 61,
      pxPerSecond: 20,
      tileWidth: 85,
      visibleFrom: 0,
      visibleTo: 100,
    });
    for (const t of tiles) expect(t.time).toBeGreaterThanOrEqual(33);
    for (const t of tiles) expect(t.time).toBeLessThan(61);
    expect(tiles[0]?.left).toBeLessThan(0); // the first tile starts before the clip and is cut by the clip's edge
  });

  it('a clip narrower than a tile has no filmstrip', () => {
    expect(filmstripTiles({ ...base, clipOut: 3, pxPerSecond: 10 })).toEqual([]);
  });

  it('builds the frame url from the asset and the time', () => {
    expect(frameUrl('a_1', 12.5)).toBe('/api/v1/assets/a_1/frame?time=12.5&width=176');
  });
});

class FakeImage implements ImageLike {
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  src = '';
}

describe('ThumbLoader', () => {
  const setup = () => {
    const images: FakeImage[] = [];
    const loader = new ThumbLoader(() => {
      const image = new FakeImage();
      images.push(image);
      return image;
    });
    return { images, loader };
  };
  const urls = (n: number) => Array.from({ length: n }, (_, i) => `/f?time=${i}`);

  it('runs a few at a time and starts the next as each one finishes', () => {
    const { images, loader } = setup();
    loader.request(urls(10));
    expect(loader.running).toBe(MAX_PARALLEL);
    expect(loader.queued).toBe(10 - MAX_PARALLEL);
    images[0]?.onload?.();
    expect(loader.isLoaded('/f?time=0')).toBe(true);
    expect(loader.running).toBe(MAX_PARALLEL);
    expect(images).toHaveLength(MAX_PARALLEL + 1);
  });

  it('a new request replaces the queue (scrolling away drops what is not wanted) but never repeats a URL', () => {
    const { images, loader } = setup();
    loader.request(urls(10));
    loader.request(['/f?time=2', '/f?time=50', '/f?time=51']);
    expect(loader.queued).toBe(2); // time=2 is already running
    images.slice(0, MAX_PARALLEL).forEach((i) => i.onload?.());
    const asked = images.map((i) => i.src);
    expect(new Set(asked).size).toBe(asked.length);
    expect(asked).toContain('/f?time=50');
    expect(asked).not.toContain('/f?time=9');
  });

  it('a failed frame is not asked for again, and onChange tells the lane to redraw', () => {
    const { images, loader } = setup();
    let changes = 0;
    loader.onChange = () => (changes += 1);
    loader.request(['/f?time=1']);
    images[0]?.onerror?.();
    expect(loader.isLoaded('/f?time=1')).toBe(false);
    loader.request(['/f?time=1']);
    expect(images).toHaveLength(1);
    expect(changes).toBe(1);
  });
});
