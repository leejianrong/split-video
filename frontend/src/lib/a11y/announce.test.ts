import { afterEach, describe, expect, it } from 'vitest';
import { announce } from './announce';

afterEach(() => {
  document.body.innerHTML = '';
});

describe('announce', () => {
  it('writes to one polite live region', () => {
    announce('Export finished');
    const region = document.getElementById('sv-live-region')!;
    expect(region.getAttribute('aria-live')).toBe('polite');
    expect(region.getAttribute('role')).toBe('status');
    expect(region.textContent?.trim()).toBe('Export finished');
    announce('Another');
    expect(document.querySelectorAll('#sv-live-region')).toHaveLength(1);
  });

  it('changes the text when the same message repeats', () => {
    announce('Saved');
    const first = document.getElementById('sv-live-region')!.textContent;
    announce('Saved');
    expect(document.getElementById('sv-live-region')!.textContent).not.toBe(first);
  });
});
