/**
 * The clips selected on the timeline (ux-spec #timeline, Selection). Not part of the project and never sent to the
 * server. The Media tab's library selection (`selection.svelte.ts`) is a separate thing.
 */
export class ClipSelectionStore {
  ids = $state<string[]>([]);

  /** The clip that last got selected, which the inspector shows when several are. */
  get primary(): string | null {
    return this.ids[this.ids.length - 1] ?? null;
  }

  has(id: string): boolean {
    return this.ids.includes(id);
  }

  /** A plain click replaces the selection; Shift-click toggles one in or out of it. */
  select(id: string, additive = false): void {
    if (!additive) this.ids = [id];
    else this.ids = this.has(id) ? this.ids.filter((x) => x !== id) : [...this.ids, id];
  }

  clear(): void {
    if (this.ids.length) this.ids = [];
  }

  /** Drop ids that are no longer on the timeline (after an edit, a reload or an agent's change). */
  prune(existing: ReadonlySet<string>): void {
    const kept = this.ids.filter((id) => existing.has(id));
    if (kept.length !== this.ids.length) this.ids = kept;
  }
}

export const clipSelection = new ClipSelectionStore();
