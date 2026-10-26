import { bindingsOf, SHORTCUTS, type Binding, type ShortcutEntry } from './shortcuts';

/** What a handler receives: the key event and the binding that matched (`j` or `l` for "J / L"). */
export interface ActionContext {
  event: KeyboardEvent;
  key: string;
}

/** Return `false` to decline the key, so it keeps its default behaviour. */
export type ActionHandler = (context: ActionContext) => void | boolean;

export type Scope = 'editor' | 'dialog';

export function detectMac(): boolean {
  if (typeof navigator === 'undefined') return false;
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  return /mac|iphone|ipad/i.test(nav.userAgentData?.platform ?? nav.platform ?? '');
}

const TEXT_INPUT_TYPES = new Set([
  'button',
  'checkbox',
  'radio',
  'submit',
  'reset',
  'image',
  'file',
  'color',
  'range',
]);
const NAVIGATION_KEYS = new Set([
  'arrowleft',
  'arrowright',
  'arrowup',
  'arrowdown',
  'home',
  'end',
  'pageup',
  'pagedown',
]);

function isTextField(el: Element | null): boolean {
  if (!el) return false;
  if (el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) return true;
  if (el instanceof HTMLInputElement) return !TEXT_INPUT_TYPES.has(el.type);
  return el instanceof HTMLElement && el.isContentEditable;
}

/** Keys the focused control already uses itself: a slider takes the arrows, a button takes Space and Enter. */
function controlOwnsKey(el: Element | null, key: string): boolean {
  if (!(el instanceof HTMLElement)) return false;
  if (el instanceof HTMLInputElement && el.type === 'range') return NAVIGATION_KEYS.has(key);
  if (key === ' ' || key === 'enter') {
    return (
      el instanceof HTMLButtonElement ||
      el instanceof HTMLAnchorElement ||
      el.tagName === 'SUMMARY' ||
      (el instanceof HTMLInputElement && TEXT_INPUT_TYPES.has(el.type)) ||
      ['button', 'tab', 'checkbox', 'switch', 'menuitem'].includes(el.getAttribute('role') ?? '')
    );
  }
  return false;
}

export interface DispatcherOptions {
  entries?: readonly ShortcutEntry[];
  /** Ctrl reads as Cmd. Defaults to detecting the platform. */
  mac?: boolean;
}

export interface Dispatcher {
  /**
   * Attach `handler` to action `id`. Throws on an unknown id and on a second live handler for the same id (two
   * components claiming one shortcut); returns the function that detaches it.
   */
  registerAction(id: string, handler: ActionHandler): () => void;
  /** Make `scope` the active scope until the returned function is called (a dialog captures the keyboard). */
  pushScope(scope: Scope): () => void;
  currentScope(): Scope;
  /** Handle one keydown. Returns true when an action ran. */
  handle(event: KeyboardEvent): boolean;
  /** Listen on `target` (the window). Returns the uninstall function. */
  install(target?: Pick<Window, 'addEventListener' | 'removeEventListener'>): () => void;
}

export function createDispatcher(options: DispatcherOptions = {}): Dispatcher {
  const entries = options.entries ?? SHORTCUTS;
  const mac = options.mac ?? detectMac();
  const handlers = new Map<string, ActionHandler>();
  const scopes: Scope[] = [];
  const parsed = entries.map((entry) => ({ entry, bindings: bindingsOf(entry) }));

  function matches(binding: Binding, event: KeyboardEvent, entry: ShortcutEntry): boolean {
    const key = event.key.toLowerCase();
    if (binding.key !== key) return false;
    // Ctrl on Windows and Linux, Cmd on macOS; the other one must be up.
    const primary = mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
    const anyPrimary = event.ctrlKey || event.metaKey;
    if (binding.primary ? !primary : anyPrimary) return false;
    if (binding.alt !== event.altKey) return false;
    // `?` and `+` are typed with Shift on some layouts, so Shift only counts for letters and named keys.
    const symbol = key.length === 1 && !/[a-z0-9]/.test(key);
    if (!symbol && !entry.allowShift && binding.shift !== event.shiftKey) return false;
    return true;
  }

  const api: Dispatcher = {
    registerAction(id, handler) {
      if (!entries.some((e) => e.id === id)) throw new Error(`unknown shortcut action "${id}"`);
      if (handlers.has(id)) throw new Error(`shortcut action "${id}" already has a handler`);
      handlers.set(id, handler);
      return () => {
        if (handlers.get(id) === handler) handlers.delete(id);
      };
    },
    pushScope(scope) {
      scopes.push(scope);
      return () => {
        const i = scopes.lastIndexOf(scope);
        if (i >= 0) scopes.splice(i, 1);
      };
    },
    currentScope: () => scopes[scopes.length - 1] ?? 'editor',
    handle(event) {
      if (event.defaultPrevented || event.isComposing) return false;
      const key = event.key.toLowerCase();
      const target = event.target instanceof Element ? event.target : null;
      const inText = isTextField(target);
      const withPrimary = mac ? event.metaKey : event.ctrlKey;
      // Text fields keep their keys, except Escape and Ctrl/Cmd combinations (ux-spec #shortcuts).
      if (inText && key !== 'escape' && !withPrimary) return false;
      if (controlOwnsKey(target, key)) return false;
      const scope = api.currentScope();
      for (const { entry, bindings } of parsed) {
        if (entry.when === 'editor' && scope !== 'editor') continue;
        if (inText && entry.nativeInText) continue;
        const binding = bindings.find((b) => matches(b, event, entry));
        const handler = handlers.get(entry.id);
        if (!binding || !handler) continue;
        if (handler({ event, key: binding.key }) === false) continue;
        event.preventDefault();
        return true;
      }
      return false;
    },
    install(target = window) {
      const listener = (event: Event) => void api.handle(event as KeyboardEvent);
      target.addEventListener('keydown', listener);
      return () => target.removeEventListener('keydown', listener);
    },
  };
  return api;
}

/** The app's dispatcher. Components use `registerAction`; `main.ts` calls `installShortcuts` once. */
export const shortcuts = createDispatcher();
export const registerAction = shortcuts.registerAction;
export const pushScope = shortcuts.pushScope;
export const installShortcuts = shortcuts.install;
export { detectMac as isMac };
