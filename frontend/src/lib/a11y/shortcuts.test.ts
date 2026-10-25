import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  bindingsOf,
  displayKeys,
  findConflicts,
  parseBinding,
  SHORTCUTS,
  SHORTCUT_GROUPS,
} from './shortcuts';

// vitest runs from frontend/.
const uxSpec = readFileSync(resolve(process.cwd(), '../docs/ux-spec.md'), 'utf8');

/** The rows of the table under "## Shortcuts" in docs/ux-spec.md: the Keys and Action cells. */
function specRows(): { keys: string[]; label: string }[] {
  const section = /^## Shortcuts\n([\s\S]*?)^## /m.exec(uxSpec)?.[1];
  if (!section) throw new Error('docs/ux-spec.md has no "## Shortcuts" section');
  const rows = section.split('\n').filter((l) => l.startsWith('|'));
  // Header and separator come first.
  return rows.slice(2).map((line) => {
    const cells = line
      .split('|')
      .slice(1, -1)
      .map((c) => c.trim());
    const [keys, label] = cells;
    if (!keys || !label) throw new Error(`bad shortcut row: ${line}`);
    return { keys: keys.replaceAll('`', '').split(' / '), label };
  });
}

describe('shortcut registry vs docs/ux-spec.md#shortcuts', () => {
  const rows = specRows();

  it('parses the whole table', () => {
    expect(rows.length).toBeGreaterThan(15);
  });

  it('has every row of the table (no row missing from the code)', () => {
    for (const row of rows) {
      const entry = SHORTCUTS.find(
        (s) => s.label === row.label && s.keys.join(' / ') === row.keys.join(' / '),
      );
      expect(
        entry,
        `ux-spec row "${row.keys.join(' / ')}: ${row.label}" has no registry entry`,
      ).toBeDefined();
    }
  });

  it('has no entry that is not in the table (no extra shortcut in the code)', () => {
    for (const entry of SHORTCUTS) {
      const row = rows.find(
        (r) => r.label === entry.label && r.keys.join(' / ') === entry.keys.join(' / '),
      );
      expect(row, `registry entry "${entry.id}" is not in the ux-spec table`).toBeDefined();
    }
  });

  it('lists the same rows as the table', () => {
    // The registry is grouped for the overlay, not ordered like the table, so compare sorted.
    const key = (r: { keys: string[]; label: string }) => `${r.keys.join(' / ')}|${r.label}`;
    expect(SHORTCUTS.map(key).sort()).toEqual(rows.map(key).sort());
    expect(SHORTCUTS).toHaveLength(rows.length);
  });
});

describe('registry integrity', () => {
  it('has unique action ids', () => {
    expect(new Set(SHORTCUTS.map((s) => s.id)).size).toBe(SHORTCUTS.length);
  });

  it('has no two entries listening for the same key', () => {
    expect(findConflicts()).toEqual([]);
  });

  it('detects a conflict when one exists', () => {
    const [a] = SHORTCUTS;
    expect(findConflicts([a!, { ...a!, id: 'other' }])).toEqual([[a!.id, 'other']]);
  });

  it('puts every entry in a known group', () => {
    for (const s of SHORTCUTS) expect(SHORTCUT_GROUPS).toContain(s.group);
  });

  it('gives every key-like entry at least one binding', () => {
    for (const s of SHORTCUTS) {
      if (s.id === 'timeline.snapOff') expect(bindingsOf(s)).toEqual([]);
      else expect(bindingsOf(s).length, s.id).toBeGreaterThan(0);
    }
  });
});

describe('parseBinding and display', () => {
  it('parses modifiers and named keys', () => {
    expect(parseBinding('Ctrl+Shift+Z')).toEqual({
      key: 'z',
      primary: true,
      shift: true,
      alt: false,
    });
    expect(parseBinding('Ctrl/Cmd+B')).toEqual({
      key: 'b',
      primary: true,
      shift: false,
      alt: false,
    });
    expect(parseBinding('Shift+Delete')).toMatchObject({ key: 'delete', shift: true });
    expect(parseBinding('Space').key).toBe(' ');
    expect(parseBinding('Comma').key).toBe(',');
    expect(parseBinding('+').key).toBe('+');
    expect(parseBinding('-').key).toBe('-');
    expect(parseBinding('?').key).toBe('?');
  });

  it('reads Ctrl as Cmd on macOS', () => {
    const undo = SHORTCUTS.find((s) => s.id === 'history.undo')!;
    expect(displayKeys(undo, false)).toEqual(['Ctrl+Z']);
    expect(displayKeys(undo, true)).toEqual(['⌘Z']);
    const split = SHORTCUTS.find((s) => s.id === 'timeline.splitAlias')!;
    expect(displayKeys(split, false)).toEqual(['Ctrl+B']);
    expect(displayKeys(split, true)).toEqual(['⌘B']);
  });
});
