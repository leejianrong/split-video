/**
 * What the user has picked in the Media tab: one library asset. Not part of the project and never sent to the server
 * (ADR 0004, "State"). The timeline cards add their own selection (clips) next to it.
 */
export class SelectionStore {
  assetId = $state<string | null>(null);

  select(id: string | null): void {
    this.assetId = id;
  }

  clear(): void {
    this.assetId = null;
  }
}

export const selection = new SelectionStore();
