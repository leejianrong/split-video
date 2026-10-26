import {
  clampOne,
  clampSizes,
  DEFAULT_SIZES,
  loadStored,
  saveStored,
  type StoreLike,
  type Resizable,
  type Sizes,
  type TabId,
  type Viewport,
} from './layout';

/**
 * Small UI state shared across the shell: overlays, the active left tab and the region sizes. Not part of the
 * project and never sent to the server; sizes and the tab are remembered per browser (ux-spec #layout).
 */
export class UiState {
  helpOpen = $state(false);
  exportOpen = $state(false);
  activeTab = $state<TabId>('media');
  /** The sizes the user chose. What is drawn is `sizes`, which also fits the window. */
  chosen = $state<Sizes>({ ...DEFAULT_SIZES });
  viewport = $state<Viewport>({ width: 1440, height: 900 });

  sizes = $derived(clampSizes(this.chosen, this.viewport));

  #storage: StoreLike | null | undefined;

  constructor(storage?: StoreLike | null | undefined) {
    this.#storage = storage;
    const stored = loadStored(storage);
    this.chosen = stored.sizes;
    this.activeTab = stored.tab;
  }

  /** Track the window; call once from the app with a `resize` listener. */
  setViewport(viewport: Viewport): void {
    this.viewport = viewport;
  }

  setSize(kind: Resizable, value: number): void {
    // Apply to what is drawn, so a drag is relative to the visible size.
    this.chosen = {
      ...this.sizes,
      [kind]: clampOne(kind, value, this.sizes, this.viewport),
    };
    this.#persist();
  }

  resetSize(kind: Resizable): void {
    this.chosen = { ...this.sizes, [kind]: DEFAULT_SIZES[kind] };
    this.#persist();
  }

  resetLayout(): void {
    this.chosen = { ...DEFAULT_SIZES };
    this.#persist();
  }

  setTab(tab: TabId): void {
    this.activeTab = tab;
    this.#persist();
  }

  #persist(): void {
    saveStored({ sizes: this.chosen, tab: this.activeTab }, this.#storage);
  }
}

export const ui = new UiState();
