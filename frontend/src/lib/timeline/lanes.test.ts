import { describe, expect, it } from 'vitest';
import type { ClipOut, ProjectOut, TrackOut } from '../api/client';
import { buildLanes, canDropOn, clipName, clipTone, laneAt, lanesHeight } from './lanes';

const clip = (id: string, extra: Partial<ClipOut> = {}): ClipOut =>
  ({
    id,
    source: 'shows/gig.mp4',
    kind: 'media',
    label: '',
    text: '',
    start: 0,
    end: 10,
    duration: 10,
    ...extra,
  }) as ClipOut;
const track = (id: string, kind: string, magnetic: boolean, clips: ClipOut[]): TrackOut =>
  ({ id, kind, magnetic, clips, locked: false, hidden: false, muted: false }) as TrackOut;
const project = (tracks: TrackOut[]): ProjectOut =>
  ({ name: 'gig.mp4', revision: 1, duration: 10, tracks }) as ProjectOut;

describe('buildLanes', () => {
  it('has the two main lanes for a plain recording and nothing else', () => {
    const lanes = buildLanes(project([track('m', 'video', true, [clip('a')])]));
    expect(lanes.map((l) => [l.name, l.kind, l.main])).toEqual([
      ['Main · Video', 'video', true],
      ['Main · Audio', 'main-audio', true],
    ]);
    expect(lanes[0]?.top).toBe(0);
    expect(lanes[1]?.top).toBe(lanes[0]?.height);
    expect(lanesHeight(lanes)).toBe((lanes[0]?.height ?? 0) + (lanes[1]?.height ?? 0));
  });

  it('puts upper tracks above (latest on top), audio below, and drops empty lanes', () => {
    const lanes = buildLanes(
      project([
        track('m', 'video', true, [clip('a')]),
        track('t1', 'video', false, [clip('x', { kind: 'text', text: 'Hi' })]),
        track('o1', 'video', false, [clip('b')]),
        track('empty', 'video', false, []),
        track('au', 'audio', false, [clip('c')]),
        track('au2', 'audio', false, [clip('d')]),
      ]),
    );
    expect(lanes.map((l) => l.name)).toEqual([
      'Overlay',
      'Text',
      'Main · Video',
      'Main · Audio',
      'Music',
      'Music 2',
    ]);
    expect(lanes.map((l) => l.id)).toEqual(['o1', 't1', 'm', 'm:audio', 'au', 'au2']);
    expect(lanes.find((l) => l.kind === 'text')?.canHide).toBe(true);
    expect(lanes.find((l) => l.kind === 'music')?.canMute).toBe(true);
    expect(lanes.find((l) => l.kind === 'video')?.canHide).toBe(false);
    expect(lanes.find((l) => l.kind === 'main-audio')?.canMute).toBe(true);
  });

  it('tops add up and laneAt finds the row under a y', () => {
    const lanes = buildLanes(
      project([track('m', 'video', true, [clip('a')]), track('au', 'audio', false, [clip('c')])]),
    );
    for (const lane of lanes) expect(laneAt(lanes, lane.top + 1)).toBe(lane);
    expect(laneAt(lanes, lanesHeight(lanes) + 5)).toBeNull();
  });

  it('is empty without a timeline', () => {
    expect(buildLanes(null)).toEqual([]);
  });
});

describe('clips', () => {
  const lanes = buildLanes(
    project([
      track('m', 'video', true, [clip('a')]),
      track('t', 'video', false, [clip('x', { kind: 'text' })]),
      track('au', 'audio', false, [clip('c')]),
    ]),
  );
  const [text, main, , music] = lanes;

  it('names a clip by its label, a title by its words, else its file', () => {
    expect(clipName(clip('a', { label: 'Song 2' }))).toBe('Song 2');
    expect(clipName(clip('a', { kind: 'text', text: 'Live at the Hall' }))).toBe(
      'Live at the Hall',
    );
    expect(clipName(clip('a'))).toBe('gig.mp4');
  });

  it('tones: songs, plain segments, titles and music', () => {
    expect(clipTone(main!, clip('a', { label: 'Song 3' }))).toBe('song');
    expect(clipTone(main!, clip('a', { label: 'Chat' }))).toBe('segment');
    expect(clipTone(text!, clip('x', { kind: 'text' }))).toBe('text');
    expect(clipTone(music!, clip('c'))).toBe('music');
  });

  it('a clip can move to another free lane of its kind, never to or from the main track or a locked lane', () => {
    const other = { ...text!, track: { ...text!.track, id: 't2' } };
    expect(canDropOn(text!, other)).toBe(true);
    expect(canDropOn(text!, music!)).toBe(false);
    expect(canDropOn(text!, main!)).toBe(false);
    expect(canDropOn(main!, text!)).toBe(false);
    expect(canDropOn(text!, { ...other, track: { ...other.track, locked: true } })).toBe(false);
  });
});
