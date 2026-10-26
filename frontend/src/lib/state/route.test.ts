import { afterEach, describe, expect, it } from 'vitest';
import { formatHash, parseHash, type Route } from './route';
import { Router } from './route.svelte';

describe('parseHash', () => {
  it('maps the empty hash and unknown hashes to the start view', () => {
    for (const hash of ['', '#', '#/', '#/nope', '#/p/', '#/p/%E0%A4%A']) {
      expect(parseHash(hash)).toEqual({ view: 'start' });
    }
  });

  it('reads the library, with and without a folder', () => {
    expect(parseHash('#/library')).toEqual({ view: 'library', dir: '' });
    expect(parseHash('#/library/gigs/2026')).toEqual({ view: 'library', dir: 'gigs/2026' });
    expect(parseHash('#/library/my%20gigs')).toEqual({ view: 'library', dir: 'my gigs' });
  });

  it('reads a project id and decodes it', () => {
    expect(parseHash('#/p/gig.mp4')).toEqual({ view: 'editor', id: 'gig.mp4' });
    expect(parseHash('#/p/gigs/a%20b%23.mp4')).toEqual({ view: 'editor', id: 'gigs/a b#.mp4' });
  });

  it('keeps the components gallery', () => {
    expect(parseHash('#/dev')).toEqual({ view: 'dev' });
  });
});

describe('formatHash', () => {
  const routes: Route[] = [
    { view: 'start' },
    { view: 'library', dir: '' },
    { view: 'library', dir: 'my gigs/2026' },
    { view: 'editor', id: 'gigs/a b#?%.mp4' },
    { view: 'dev' },
  ];
  it.each(routes)('round-trips %o', (route) => {
    expect(parseHash(formatHash(route))).toEqual(route);
  });

  it('writes the documented shapes', () => {
    expect(formatHash({ view: 'editor', id: 'a/b c.mp4' })).toBe('#/p/a/b%20c.mp4');
    expect(formatHash({ view: 'library', dir: '' })).toBe('#/library');
  });
});

describe('Router', () => {
  let stop: (() => void) | undefined;
  afterEach(() => {
    stop?.();
    location.hash = '';
  });

  it('follows the hash, including the back button', async () => {
    const router = new Router();
    stop = router.listen();
    expect(router.current).toEqual({ view: 'start' });
    router.go({ view: 'editor', id: 'x.mp4' });
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(router.current).toEqual({ view: 'editor', id: 'x.mp4' });
    location.hash = '#/library';
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(router.current).toEqual({ view: 'library', dir: '' });
  });

  it('reads the hash it starts with (a reload reopens the same project)', () => {
    location.hash = '#/p/gigs/one.mp4';
    const router = new Router();
    stop = router.listen();
    expect(router.current).toEqual({ view: 'editor', id: 'gigs/one.mp4' });
  });
});
