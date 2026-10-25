import { describe, expect, it } from 'vitest';
import { DEFAULT_SIZES, STORAGE_KEY } from './layout';
import { UiState } from './ui.svelte';

const memory = () => {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    data,
  };
};

describe('UiState', () => {
  it('starts at the wireframe sizes and the Media tab', () => {
    const ui = new UiState(memory());
    expect(ui.sizes).toEqual(DEFAULT_SIZES);
    expect(ui.activeTab).toBe('media');
  });

  it('clamps a resize, persists it, and restores it in a new session', () => {
    const store = memory();
    const ui = new UiState(store);
    ui.setSize('panel', 400);
    ui.setSize('inspector', 5000);
    ui.setTab('transcript');
    expect(ui.sizes.panel).toBe(400);
    expect(ui.sizes.inspector).toBe(440); // the inspector's own maximum
    const again = new UiState(store);
    expect(again.sizes.panel).toBe(400);
    expect(again.activeTab).toBe('transcript');
    expect(JSON.parse(store.data.get(STORAGE_KEY) ?? '{}').sizes.panel).toBe(400);
  });

  it('never lets a drag take the preview under 480 px', () => {
    const ui = new UiState(memory());
    ui.setSize('panel', 480);
    ui.setSize('inspector', 5000);
    expect(1440 - 72 - ui.sizes.panel - ui.sizes.inspector).toBe(480);
  });

  it('resets one region or all of them', () => {
    const ui = new UiState(memory());
    ui.setSize('panel', 420);
    ui.setSize('timeline', 400);
    ui.resetSize('panel');
    expect(ui.sizes.panel).toBe(DEFAULT_SIZES.panel);
    expect(ui.sizes.timeline).toBe(400);
    ui.resetLayout();
    expect(ui.sizes).toEqual(DEFAULT_SIZES);
  });

  it('draws sizes that fit a narrower window without forgetting the chosen ones', () => {
    const ui = new UiState(memory());
    ui.setSize('panel', 480);
    ui.setViewport({ width: 1280, height: 720 });
    expect(1280 - 72 - ui.sizes.panel - ui.sizes.inspector).toBeGreaterThanOrEqual(480);
    ui.setViewport({ width: 1920, height: 1080 });
    expect(ui.sizes.panel).toBe(480);
  });
});
