import { afterEach, describe, expect, it, vi } from 'vitest';
import { createDispatcher } from './dispatcher';

function key(k: string, init: KeyboardEventInit = {}, target?: Element): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...init });
  if (target) Object.defineProperty(event, 'target', { value: target });
  return event;
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, string> = {},
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  document.body.append(node);
  return node;
}

afterEach(() => {
  document.body.innerHTML = '';
});

describe('dispatcher', () => {
  it('runs the handler of the matching action and prevents the default', () => {
    const d = createDispatcher({ mac: false });
    const handler = vi.fn();
    d.registerAction('timeline.split', handler);
    const event = key('s');
    expect(d.handle(event)).toBe(true);
    expect(handler).toHaveBeenCalledOnce();
    expect(event.defaultPrevented).toBe(true);
  });

  it('passes the matched key for grouped bindings', () => {
    const d = createDispatcher({ mac: false });
    const seen: string[] = [];
    d.registerAction('transport.step', ({ key: k }) => void seen.push(k));
    d.handle(key('j'));
    d.handle(key('L'));
    expect(seen).toEqual(['j', 'l']);
  });

  it('ignores keys with no registered handler and does not prevent the default', () => {
    const d = createDispatcher({ mac: false });
    const event = key('s');
    expect(d.handle(event)).toBe(false);
    expect(event.defaultPrevented).toBe(false);
  });

  it('lets a handler decline with false', () => {
    const d = createDispatcher({ mac: false });
    d.registerAction('timeline.split', () => false);
    const event = key('s');
    expect(d.handle(event)).toBe(false);
    expect(event.defaultPrevented).toBe(false);
  });

  it('skips keys already handled by a component (defaultPrevented)', () => {
    const d = createDispatcher({ mac: false });
    const handler = vi.fn();
    d.registerAction('timeline.split', handler);
    const event = key('s');
    event.preventDefault();
    d.handle(event);
    expect(handler).not.toHaveBeenCalled();
  });

  describe('text fields', () => {
    it('skip plain keys', () => {
      const d = createDispatcher({ mac: false });
      const handler = vi.fn();
      d.registerAction('timeline.split', handler);
      for (const field of [
        el('input', { type: 'text' }),
        el('textarea'),
        el('select'),
        el('input'),
      ]) {
        expect(d.handle(key('s', {}, field))).toBe(false);
      }
      const editable = el('div');
      Object.defineProperty(editable, 'isContentEditable', { value: true });
      expect(d.handle(key('s', {}, editable))).toBe(false);
      expect(handler).not.toHaveBeenCalled();
    });

    it('still get Escape and Ctrl combinations', () => {
      const d = createDispatcher({ mac: false });
      const escape = vi.fn();
      const save = vi.fn();
      d.registerAction('ui.escape', escape);
      d.registerAction('project.save', save);
      const input = el('input', { type: 'text' });
      d.handle(key('Escape', {}, input));
      d.handle(key('s', { ctrlKey: true }, input));
      expect(escape).toHaveBeenCalledOnce();
      expect(save).toHaveBeenCalledOnce();
    });

    it('keep their own undo, redo and select all', () => {
      const d = createDispatcher({ mac: false });
      const undo = vi.fn();
      d.registerAction('history.undo', undo);
      const input = el('input', { type: 'text' });
      expect(d.handle(key('z', { ctrlKey: true }, input))).toBe(false);
      expect(d.handle(key('z', { ctrlKey: true }, el('div')))).toBe(true);
      expect(undo).toHaveBeenCalledOnce();
    });

    it('do not swallow keys on checkboxes, but a slider keeps its arrows and a button its Space', () => {
      const d = createDispatcher({ mac: false });
      const nudge = vi.fn();
      const toggle = vi.fn();
      const split = vi.fn();
      d.registerAction('timeline.nudge', nudge);
      d.registerAction('transport.toggle', toggle);
      d.registerAction('timeline.split', split);
      expect(d.handle(key('ArrowLeft', {}, el('input', { type: 'range' })))).toBe(false);
      expect(d.handle(key(' ', {}, el('button')))).toBe(false);
      expect(d.handle(key('s', {}, el('input', { type: 'checkbox' })))).toBe(true);
      expect(toggle).not.toHaveBeenCalled();
      expect(nudge).not.toHaveBeenCalled();
      expect(split).toHaveBeenCalledOnce();
    });
  });

  describe('modifiers', () => {
    it('requires the modifiers a binding names and rejects extra ones', () => {
      const d = createDispatcher({ mac: false });
      const split = vi.fn();
      const alias = vi.fn();
      d.registerAction('timeline.split', split);
      d.registerAction('timeline.splitAlias', alias);
      expect(d.handle(key('s', { ctrlKey: true }))).toBe(false);
      expect(d.handle(key('s', { altKey: true }))).toBe(false);
      expect(d.handle(key('b', { ctrlKey: true }))).toBe(true);
      expect(d.handle(key('b'))).toBe(false);
      expect(split).not.toHaveBeenCalled();
      expect(alias).toHaveBeenCalledOnce();
    });

    it('tells Delete from Shift+Delete and undo from redo', () => {
      const d = createDispatcher({ mac: false });
      const del = vi.fn();
      const ripple = vi.fn();
      const undo = vi.fn();
      const redo = vi.fn();
      d.registerAction('timeline.delete', del);
      d.registerAction('timeline.rippleDelete', ripple);
      d.registerAction('history.undo', undo);
      d.registerAction('history.redo', redo);
      d.handle(key('Delete'));
      d.handle(key('Delete', { shiftKey: true }));
      d.handle(key('z', { ctrlKey: true }));
      d.handle(key('Z', { ctrlKey: true, shiftKey: true }));
      d.handle(key('y', { ctrlKey: true }));
      expect([del.mock.calls.length, ripple.mock.calls.length]).toEqual([1, 1]);
      expect([undo.mock.calls.length, redo.mock.calls.length]).toEqual([1, 2]);
    });

    it('lets a nudge arrive with or without Shift', () => {
      const d = createDispatcher({ mac: false });
      const nudge = vi.fn();
      d.registerAction('timeline.nudge', nudge);
      d.handle(key('ArrowLeft'));
      d.handle(key('ArrowRight', { shiftKey: true }));
      expect(nudge).toHaveBeenCalledTimes(2);
    });

    it('does not need Shift for ? or +', () => {
      const d = createDispatcher({ mac: false });
      const help = vi.fn();
      const zoom = vi.fn();
      d.registerAction('help.toggle', help);
      d.registerAction('timeline.zoom', zoom);
      d.handle(key('?', { shiftKey: true }));
      d.handle(key('+', { shiftKey: true }));
      d.handle(key('=', {}));
      d.handle(key('-', {}));
      expect(help).toHaveBeenCalledOnce();
      expect(zoom).toHaveBeenCalledTimes(3);
    });
  });

  describe('macOS', () => {
    it('maps Ctrl to Cmd', () => {
      const d = createDispatcher({ mac: true });
      const undo = vi.fn();
      d.registerAction('history.undo', undo);
      expect(d.handle(key('z', { ctrlKey: true }))).toBe(false);
      expect(d.handle(key('z', { metaKey: true }))).toBe(true);
      expect(undo).toHaveBeenCalledOnce();
    });

    it('does not map Cmd on other platforms', () => {
      const d = createDispatcher({ mac: false });
      d.registerAction('history.undo', vi.fn());
      expect(d.handle(key('z', { metaKey: true }))).toBe(false);
    });
  });

  describe('scopes', () => {
    it('keeps editor shortcuts out of dialogs but leaves Escape and ? alone', () => {
      const d = createDispatcher({ mac: false });
      const split = vi.fn();
      const escape = vi.fn();
      d.registerAction('timeline.split', split);
      d.registerAction('ui.escape', escape);
      const pop = d.pushScope('dialog');
      expect(d.currentScope()).toBe('dialog');
      expect(d.handle(key('s'))).toBe(false);
      expect(d.handle(key('Escape'))).toBe(true);
      pop();
      expect(d.currentScope()).toBe('editor');
      expect(d.handle(key('s'))).toBe(true);
      expect(split).toHaveBeenCalledOnce();
      expect(escape).toHaveBeenCalledOnce();
    });

    it('nests: closing the inner dialog keeps the outer one captured', () => {
      const d = createDispatcher({ mac: false });
      const popOuter = d.pushScope('dialog');
      const popInner = d.pushScope('dialog');
      popInner();
      expect(d.currentScope()).toBe('dialog');
      popOuter();
      expect(d.currentScope()).toBe('editor');
    });
  });

  describe('registration', () => {
    it('rejects a second handler for the same action and an unknown action', () => {
      const d = createDispatcher({ mac: false });
      d.registerAction('timeline.split', vi.fn());
      expect(() => d.registerAction('timeline.split', vi.fn())).toThrow(/already has a handler/);
      expect(() => d.registerAction('nope', vi.fn())).toThrow(/unknown shortcut action/);
    });

    it('allows registering again after the first one is released', () => {
      const d = createDispatcher({ mac: false });
      const release = d.registerAction('timeline.split', vi.fn());
      release();
      const again = vi.fn();
      d.registerAction('timeline.split', again);
      d.handle(key('s'));
      expect(again).toHaveBeenCalledOnce();
    });

    it('a stale release does not remove a newer handler', () => {
      const d = createDispatcher({ mac: false });
      const release = d.registerAction('timeline.split', vi.fn());
      release();
      const next = vi.fn();
      d.registerAction('timeline.split', next);
      release();
      d.handle(key('s'));
      expect(next).toHaveBeenCalledOnce();
    });
  });

  it('install listens on the target and uninstall stops it', () => {
    const d = createDispatcher({ mac: false });
    const handler = vi.fn();
    d.registerAction('timeline.split', handler);
    const uninstall = d.install(window);
    window.dispatchEvent(key('s'));
    uninstall();
    window.dispatchEvent(key('s'));
    expect(handler).toHaveBeenCalledOnce();
  });
});
