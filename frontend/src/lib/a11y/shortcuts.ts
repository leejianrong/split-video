/**
 * The shortcut registry: every keyboard shortcut of the editor, as data, in one place.
 *
 * It defines exactly the table in docs/ux-spec.md#shortcuts; shortcuts.test.ts parses that table and fails when
 * either side drifts. `keys` and `label` are the table's Keys and Action cells verbatim. To add a shortcut: add the
 * row to the ux-spec table, add the entry here, call `registerAction(id, handler)` from the component that owns it.
 * Tooltips and the `?` overlay read this file, so they cannot disagree with it.
 */

/** Where a shortcut is live: `editor` only with no dialog open, `any` also inside dialogs. */
export type When = 'editor' | 'any';

export type ShortcutGroup =
  'Playback' | 'Editing' | 'Navigation' | 'Timeline view' | 'Project' | 'General';

export interface ShortcutEntry {
  /** Stable action id; components register a handler under it. */
  id: string;
  /** The Keys cell of the ux-spec table, split at " / ". */
  keys: string[];
  /** The Action cell of the ux-spec table. */
  label: string;
  group: ShortcutGroup;
  when: When;
  /**
   * What the dispatcher listens for, as binding strings ("ctrl+shift+z", "arrowleft", " "). Defaults to the
   * bindings parsed from `keys`; set it where `keys` is prose (the zoom wheel) or has no key (Alt while dragging).
   */
  bindings?: string[];
  /** The same binding fires with Shift held (the handler reads `event.shiftKey`): nudge 0.1 s or 1 s. */
  allowShift?: boolean;
  /** Ctrl/Cmd combination that text fields keep for themselves (undo, redo, select all), so typing stays sane. */
  nativeInText?: boolean;
}

export const SHORTCUT_GROUPS: readonly ShortcutGroup[] = [
  'Playback',
  'Editing',
  'Navigation',
  'Timeline view',
  'Project',
  'General',
];

export const SHORTCUTS: readonly ShortcutEntry[] = [
  {
    id: 'transport.toggle',
    keys: ['Space', 'K'],
    label: 'Play / pause',
    group: 'Playback',
    when: 'editor',
  },
  {
    id: 'transport.step',
    keys: ['J', 'L'],
    label: 'Step back / forward 5 s',
    group: 'Playback',
    when: 'editor',
  },
  {
    id: 'timeline.split',
    keys: ['S'],
    label: 'Split at the playhead (adds a cut)',
    group: 'Editing',
    when: 'editor',
  },
  {
    id: 'timeline.splitAlias',
    keys: ['Ctrl/Cmd+B'],
    label: 'Split at the playhead',
    group: 'Editing',
    when: 'editor',
  },
  {
    id: 'timeline.delete',
    keys: ['Delete', 'Backspace'],
    label: 'Delete the selected clip or cut (a cut: remove the cut)',
    group: 'Editing',
    when: 'editor',
  },
  {
    id: 'timeline.rippleDelete',
    keys: ['Shift+Delete'],
    label: 'Ripple delete: delete the selection and close the gap',
    group: 'Editing',
    when: 'editor',
  },
  {
    id: 'timeline.nudge',
    keys: ['Left', 'Right'],
    label: 'Nudge the selected cut 0.1 s (1 s with Shift)',
    group: 'Editing',
    when: 'editor',
    bindings: ['arrowleft', 'arrowright'],
    allowShift: true,
  },
  {
    id: 'transport.frame',
    keys: ['Comma', 'Period'],
    label: 'Previous / next frame',
    group: 'Navigation',
    when: 'editor',
  },
  {
    id: 'transport.edge',
    keys: ['Home', 'End'],
    label: 'Playhead to start / end',
    group: 'Navigation',
    when: 'editor',
  },
  {
    id: 'transport.cut',
    keys: ['Up', 'Down'],
    label: 'Playhead to previous / next cut',
    group: 'Navigation',
    when: 'editor',
    bindings: ['arrowup', 'arrowdown'],
  },
  {
    id: 'history.undo',
    keys: ['Ctrl+Z'],
    label: 'Undo',
    group: 'Editing',
    when: 'editor',
    nativeInText: true,
  },
  {
    id: 'history.redo',
    keys: ['Ctrl+Shift+Z', 'Ctrl+Y'],
    label: 'Redo',
    group: 'Editing',
    when: 'editor',
    nativeInText: true,
  },
  {
    id: 'selection.all',
    keys: ['Ctrl+A'],
    label: 'Select all clips on unlocked lanes',
    group: 'Editing',
    when: 'editor',
    nativeInText: true,
  },
  {
    id: 'selection.duplicate',
    keys: ['Ctrl+D'],
    label: 'Duplicate the selection',
    group: 'Editing',
    when: 'editor',
  },
  { id: 'project.save', keys: ['Ctrl+S'], label: 'Save now', group: 'Project', when: 'editor' },
  {
    id: 'project.export',
    keys: ['Ctrl+E'],
    label: 'Open Export',
    group: 'Project',
    when: 'editor',
  },
  {
    id: 'timeline.zoom',
    keys: ['Ctrl + wheel, +', '-'],
    label: 'Zoom the timeline',
    group: 'Timeline view',
    when: 'editor',
    // The wheel is a pointer gesture; `+` also types as `=` without Shift on most layouts.
    bindings: ['+', '=', '-'],
  },
  {
    id: 'timeline.fit',
    keys: ['Shift+Z'],
    label: 'Fit the whole timeline',
    group: 'Timeline view',
    when: 'editor',
  },
  {
    id: 'timeline.snapping',
    keys: ['N'],
    label: 'Toggle snapping',
    group: 'Timeline view',
    when: 'editor',
  },
  {
    id: 'timeline.snapOff',
    keys: ['Alt (held while dragging)'],
    label: 'Snapping off for that drag',
    group: 'Timeline view',
    when: 'editor',
    // Read from the drag's pointer events by the timeline, never dispatched as a key.
    bindings: [],
  },
  {
    id: 'ui.escape',
    keys: ['Escape'],
    label: 'Close dialog, clear selection',
    group: 'General',
    when: 'any',
  },
  {
    id: 'help.toggle',
    keys: ['?'],
    label: 'Toggle the shortcut list',
    group: 'General',
    when: 'any',
  },
];

/** A parsed binding: the key (lower case, `event.key` spelling) and the modifiers it needs. */
export interface Binding {
  key: string;
  /** Ctrl on Windows and Linux, Cmd on macOS. */
  primary: boolean;
  shift: boolean;
  alt: boolean;
}

const KEY_NAMES: Record<string, string> = {
  space: ' ',
  comma: ',',
  period: '.',
  left: 'arrowleft',
  right: 'arrowright',
  up: 'arrowup',
  down: 'arrowdown',
  esc: 'escape',
};

/** "Ctrl/Cmd+B", "Shift+Delete", "Comma", "+" to a Binding. */
export function parseBinding(text: string): Binding {
  const binding: Binding = { key: '', primary: false, shift: false, alt: false };
  // A trailing "+" is the plus key itself ("+", "Ctrl++"), not a separator.
  const parts =
    text === '+'
      ? ['+']
      : text.endsWith('++')
        ? [...text.slice(0, -2).split('+'), '+']
        : text.split('+');
  for (const raw of parts) {
    const part = raw.trim().toLowerCase();
    if (part === 'ctrl' || part === 'cmd' || part === 'ctrl/cmd' || part === 'meta')
      binding.primary = true;
    else if (part === 'shift') binding.shift = true;
    else if (part === 'alt') binding.alt = true;
    else binding.key = KEY_NAMES[part] ?? part;
  }
  if (!binding.key) throw new Error(`binding without a key: "${text}"`);
  return binding;
}

/** The binding strings an entry listens for (explicit `bindings`, else parsed from `keys`). */
export function bindingsOf(entry: ShortcutEntry): Binding[] {
  return (entry.bindings ?? entry.keys).map(parseBinding);
}

export function findShortcut(id: string): ShortcutEntry | undefined {
  return SHORTCUTS.find((s) => s.id === id);
}

function sameBinding(a: Binding, b: Binding): boolean {
  return a.key === b.key && a.primary === b.primary && a.shift === b.shift && a.alt === b.alt;
}

/** Two entries that listen for the same key in an overlapping context, for the conflict check. */
export function findConflicts(entries: readonly ShortcutEntry[] = SHORTCUTS): [string, string][] {
  const conflicts: [string, string][] = [];
  const seenIds = new Set<string>();
  for (const [i, a] of entries.entries()) {
    if (seenIds.has(a.id)) conflicts.push([a.id, a.id]);
    seenIds.add(a.id);
    for (const b of entries.slice(i + 1)) {
      // `editor` and `any` overlap in the editor scope; the only disjoint pair would be two dialog-only entries.
      const clash = bindingsOf(a).some((x) => bindingsOf(b).some((y) => sameBinding(x, y)));
      if (clash) conflicts.push([a.id, b.id]);
    }
  }
  return conflicts;
}

/** Text for display: "Ctrl" is "⌘" on macOS (ux-spec: on macOS Ctrl reads as Cmd), "Ctrl/Cmd" collapses. */
export function formatKey(key: string, mac: boolean): string {
  const text = key.replace('Ctrl/Cmd', 'Ctrl');
  return mac ? text.replace(/Ctrl\s*\+\s*(?=wheel)/, '⌘ + ').replace(/Ctrl\+?/g, '⌘') : text;
}

/** Display strings of an entry's keys. */
export function displayKeys(entry: ShortcutEntry, mac: boolean): string[] {
  return entry.keys.map((k) => formatKey(k, mac));
}
