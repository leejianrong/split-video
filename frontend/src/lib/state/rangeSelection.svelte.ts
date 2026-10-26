/**
 * A range of a recording the user is looking at (the highlight card under the pointer or selected), in seconds of
 * the source. The timeline draws it over the lanes; it is not part of the project and never sent to the server.
 */
export interface SourceRange {
  assetId: string;
  start: number;
  end: number;
}

export class RangeSelectionStore {
  range = $state<SourceRange | null>(null);

  set(range: SourceRange | null): void {
    this.range = range;
  }
}

export const rangeSelection = new RangeSelectionStore();
