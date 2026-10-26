/**
 * The drag payload for a library asset: what the Media tab puts on a drag and what a drop target (the timeline) reads.
 * One contract so the timeline card (SPL-65) can accept the same drag without knowing about the Media tab.
 *
 * - MIME type `application/x-split-video-asset`: JSON `{"v":1,"assetId":"a_...","name":"gig.mp4","duration":9668.2}`.
 * - Also `text/plain` with the file name, so dropping onto a text field or another app gives something readable.
 * - `effectAllowed` is `copy`: dragging an asset never moves or removes it from the library.
 *
 * While a drag is over a target only the MIME *types* are readable (browsers hide the data until the drop), so a
 * target checks `hasAssetDrag` on `dragenter` / `dragover`, calls `preventDefault()` to accept, and reads the payload
 * with `readAssetDrag` on `drop`. The keyboard and button alternative is `project.addAsset(assetId)`.
 */
export const ASSET_MIME = 'application/x-split-video-asset';

export interface AssetDragPayload {
  v: 1;
  assetId: string;
  name: string;
  duration: number;
}

/** The slice of `DataTransfer` the helpers use, so tests need no browser drag. */
export interface TransferLike {
  types: readonly string[] | DOMStringList;
  getData(type: string): string;
  setData(type: string, value: string): void;
  effectAllowed?: string;
  dropEffect?: string;
}

export function setAssetDrag(
  transfer: TransferLike,
  asset: { id: string; name: string; duration: number },
): void {
  const payload: AssetDragPayload = {
    v: 1,
    assetId: asset.id,
    name: asset.name,
    duration: asset.duration,
  };
  transfer.setData(ASSET_MIME, JSON.stringify(payload));
  transfer.setData('text/plain', asset.name);
  transfer.effectAllowed = 'copy';
}

/** Whether a drag in progress carries an asset (readable during dragover; the data itself is not). */
export function hasAssetDrag(transfer: Pick<TransferLike, 'types'> | null | undefined): boolean {
  return !!transfer && Array.from(transfer.types as ArrayLike<string>).includes(ASSET_MIME);
}

/** The asset on a drop, or null for anything else (a file from the desktop, text, a malformed payload). */
export function readAssetDrag(
  transfer: Pick<TransferLike, 'getData'> | null,
): AssetDragPayload | null {
  if (!transfer) return null;
  try {
    const parsed: unknown = JSON.parse(transfer.getData(ASSET_MIME));
    if (typeof parsed !== 'object' || parsed === null) return null;
    const p = parsed as Record<string, unknown>;
    if (p.v !== 1 || typeof p.assetId !== 'string' || !p.assetId) return null;
    return {
      v: 1,
      assetId: p.assetId,
      name: typeof p.name === 'string' ? p.name : '',
      duration: typeof p.duration === 'number' ? p.duration : 0,
    };
  } catch {
    return null;
  }
}

/** Whether a drag carries files from the operating system (the Media tab imports those). */
export function hasFileDrag(transfer: Pick<TransferLike, 'types'> | null | undefined): boolean {
  return !!transfer && Array.from(transfer.types as ArrayLike<string>).includes('Files');
}
