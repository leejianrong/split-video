import { describe, expect, it } from 'vitest';
import {
  ASSET_MIME,
  hasAssetDrag,
  hasFileDrag,
  readAssetDrag,
  setAssetDrag,
  type TransferLike,
} from './dnd';

function transfer(): TransferLike & { data: Record<string, string> } {
  const data: Record<string, string> = {};
  return {
    data,
    get types() {
      return Object.keys(data);
    },
    getData: (type) => data[type] ?? '',
    setData: (type, value) => void (data[type] = value),
  };
}

describe('asset drag payload', () => {
  it('round-trips through the custom MIME type and offers a plain-text name', () => {
    const t = transfer();
    setAssetDrag(t, { id: 'a_1', name: 'gig.mp4', duration: 9668.2 });
    expect(Object.keys(t.data)).toContain(ASSET_MIME);
    expect(t.data['text/plain']).toBe('gig.mp4');
    expect(t.effectAllowed).toBe('copy');
    expect(hasAssetDrag(t)).toBe(true);
    expect(readAssetDrag(t)).toEqual({ v: 1, assetId: 'a_1', name: 'gig.mp4', duration: 9668.2 });
  });

  it('is not fooled by other drags or a broken payload', () => {
    const files = transfer();
    files.data.Files = '';
    expect(hasAssetDrag(files)).toBe(false);
    expect(hasFileDrag(files)).toBe(true);
    expect(readAssetDrag(files)).toBeNull();
    for (const bad of ['', 'not json', '[]', '{"v":2,"assetId":"a"}', '{"v":1}', 'null']) {
      const t = transfer();
      t.data[ASSET_MIME] = bad;
      expect(readAssetDrag(t), bad).toBeNull();
    }
    expect(readAssetDrag(null)).toBeNull();
    expect(hasAssetDrag(null)).toBe(false);
  });
});
