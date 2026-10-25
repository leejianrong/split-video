import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_DURATION, ToastStore } from './toasts.svelte';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('ToastStore', () => {
  it('dismisses info, success and warning after the default duration', () => {
    const store = new ToastStore();
    for (const kind of ['info', 'success', 'warning'] as const) store.show({ kind, message: kind });
    expect(store.items).toHaveLength(3);
    vi.advanceTimersByTime(DEFAULT_DURATION + 1);
    expect(store.items).toHaveLength(0);
  });

  it('keeps errors until dismissed', () => {
    const store = new ToastStore();
    const id = store.error('Export failed');
    vi.advanceTimersByTime(10 * 60 * 1000);
    expect(store.items.map((t) => t.id)).toEqual([id]);
    store.dismiss(id);
    expect(store.items).toHaveLength(0);
  });

  it('honours an explicit duration, including null for a sticky non-error', () => {
    const store = new ToastStore();
    store.show({ message: 'short', duration: 1000 });
    store.show({ message: 'sticky', duration: null });
    vi.advanceTimersByTime(1001);
    expect(store.items.map((t) => t.message)).toEqual(['sticky']);
  });

  it('pauses the timer while hovered and resumes with the time that was left', () => {
    const store = new ToastStore();
    const id = store.show({ message: 'hello', duration: 1000 });
    vi.advanceTimersByTime(600);
    store.pause(id);
    vi.advanceTimersByTime(60_000);
    expect(store.items).toHaveLength(1);
    store.resume(id);
    vi.advanceTimersByTime(399);
    expect(store.items).toHaveLength(1);
    vi.advanceTimersByTime(2);
    expect(store.items).toHaveLength(0);
  });

  it('keeps the action and gives each toast its own id', () => {
    const store = new ToastStore();
    const onclick = vi.fn();
    const a = store.show({ message: 'a', action: { label: 'Undo', onclick } });
    const b = store.show({ message: 'b' });
    expect(a).not.toBe(b);
    store.items[0]?.action?.onclick();
    expect(onclick).toHaveBeenCalledOnce();
  });

  it('dismissing one leaves the others and their timers alone', () => {
    const store = new ToastStore();
    const a = store.show({ message: 'a', duration: 1000 });
    store.show({ message: 'b', duration: 1000 });
    store.dismiss(a);
    expect(store.items.map((t) => t.message)).toEqual(['b']);
    vi.advanceTimersByTime(1001);
    expect(store.items).toHaveLength(0);
  });
});
