import { describe, expect, it } from 'vitest';
import { SelectionStore } from './selection.svelte';

describe('SelectionStore', () => {
  it('selects one asset, replaces it, and clears', () => {
    const s = new SelectionStore();
    expect(s.assetId).toBeNull();
    s.select('a_1');
    s.select('a_2');
    expect(s.assetId).toBe('a_2');
    s.clear();
    expect(s.assetId).toBeNull();
  });
});
