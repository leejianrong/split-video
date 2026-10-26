/**
 * The lanes of the timeline, built from the project's tracks (ux-spec #timeline): layout only. Which clips exist, where
 * they sit and what a lock does are the engine's; this turns them into rows.
 *
 * Top to bottom: overlay and text tracks (the later the track, the higher: it draws on top), then the main video
 * track and the main track's audio, then audio tracks ("Music"). Lanes appear only when they have content, except the
 * two main lanes (ux-spec decision 13).
 */
import type { ClipOut, ProjectOut, TrackOut } from '../api/client';

export type LaneKind = 'text' | 'overlay' | 'video' | 'main-audio' | 'music';

export interface Lane {
  /** Stable element id: the track id, with `:audio` for the main track's audio lane. */
  id: string;
  kind: LaneKind;
  /** "Text", "Main · Video", "Main · Audio", "Music" (numbered when there are several). */
  name: string;
  track: TrackOut;
  clips: readonly ClipOut[];
  /** The lane's row height in px. */
  height: number;
  /** Top of the row inside the lanes area, in px. */
  top: number;
  /** Which toggles the header offers: video lanes hide, audio lanes mute (the main track's audio lane mutes it). */
  canHide: boolean;
  canMute: boolean;
  main: boolean;
}

export const LANE_HEIGHT: Record<LaneKind, number> = {
  text: 40,
  overlay: 40,
  video: 56,
  'main-audio': 40,
  music: 40,
};

/** The main track: the first video track, the one the engine keeps back to back. */
export function mainTrack(timeline: ProjectOut | null): TrackOut | undefined {
  return timeline?.tracks.find((t) => t.kind === 'video');
}

function numbered(base: string, count: Map<string, number>): string {
  const n = (count.get(base) ?? 0) + 1;
  count.set(base, n);
  return n === 1 ? base : `${base} ${n}`;
}

export function buildLanes(timeline: ProjectOut | null): Lane[] {
  if (!timeline) return [];
  const main = mainTrack(timeline);
  const rows: Omit<Lane, 'top'>[] = [];
  const counts = new Map<string, number>();
  const make = (
    track: TrackOut,
    kind: LaneKind,
    name: string,
    extra: Partial<Lane> = {},
  ): Omit<Lane, 'top'> => ({
    id: track.id,
    kind,
    name,
    track,
    clips: track.clips,
    height: LANE_HEIGHT[kind],
    canHide: kind === 'text' || kind === 'overlay',
    canMute: kind === 'music' || kind === 'main-audio',
    main: false,
    ...extra,
  });

  // Upper video tracks, the latest (top of the stack) first.
  const upper = timeline.tracks.filter(
    (t) => t.kind === 'video' && t !== main && t.clips.length > 0,
  );
  for (const track of [...upper].reverse()) {
    const isText = track.clips.every((c) => c.kind === 'text');
    const kind: LaneKind = isText ? 'text' : 'overlay';
    rows.push(make(track, kind, numbered(isText ? 'Text' : 'Overlay', counts)));
  }
  if (main) {
    rows.push(make(main, 'video', 'Main · Video', { main: true, canHide: false }));
    rows.push(
      make(main, 'main-audio', 'Main · Audio', {
        id: `${main.id}:audio`,
        main: true,
        canHide: false,
      }),
    );
  }
  for (const track of timeline.tracks.filter((t) => t.kind === 'audio' && t.clips.length > 0)) {
    rows.push(make(track, 'music', numbered('Music', counts)));
  }
  let top = 0;
  return rows.map((row) => {
    const lane = { ...row, top };
    top += row.height;
    return lane;
  });
}

/** Total height of the rows. */
export function lanesHeight(lanes: readonly Lane[]): number {
  const last = lanes[lanes.length - 1];
  return last ? last.top + last.height : 0;
}

/** The lane under a y offset inside the lanes area, or null below the last one. */
export function laneAt(lanes: readonly Lane[], y: number): Lane | null {
  return lanes.find((lane) => y >= lane.top && y < lane.top + lane.height) ?? null;
}

/** Whether a clip may be dragged to `target`: free tracks of its own kind (the main track reorders instead). */
export function canDropOn(from: Lane, target: Lane): boolean {
  if (from.main || target.main) return false;
  return from.track.kind === target.track.kind && !target.track.locked;
}

/** Segment names that the analysis produces get their own colour; everything else uses the plain one. */
export type ClipTone = 'song' | 'segment' | 'text' | 'overlay' | 'music';

export function clipTone(lane: Lane, clip: ClipOut): ClipTone {
  if (lane.kind === 'music') return 'music';
  if (lane.kind === 'text' || clip.kind === 'text') return 'text';
  if (lane.kind === 'overlay') return 'overlay';
  return /^song\b/i.test(clip.label ?? '') ? 'song' : 'segment';
}

/** The text on a clip: its label, a title's words, else the file name. */
export function clipName(clip: ClipOut): string {
  if (clip.label) return clip.label;
  if (clip.kind === 'text') return clip.text || 'Title';
  const name = clip.source.slice(clip.source.lastIndexOf('/') + 1);
  return name || 'Clip';
}
