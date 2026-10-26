/**
 * What a drag or a key press on a clip turns into: the engine's edit operations (docs/edit-ops.md) and, while the
 * pointer is down, the geometry to draw. Layout maths only: the engine checks every rule (overlaps, lengths, locks) and
 * says no with a code if the clamps here missed one.
 */
import type { AssetOut, ClipOut } from '../api/client';
import type { Lane } from './lanes';

/** One operation of an edit-ops document (`POST /project/ops`). */
export type EditOp = { op: string } & Record<string, unknown>;

/** The shortest clip the engine accepts. */
export const MIN_CLIP = 0.1;

const ms = (seconds: number): number => Math.round(seconds * 1000) / 1000;

export type Edge = 'start' | 'end';

export interface TrimLimits {
  /** How long the source is (null for a title, which has no source). */
  sourceDuration: number | null;
  /** Free tracks only: the neighbours' edges on the timeline, so a trim cannot run into them. */
  leftLimit: number | null;
  rightLimit: number | null;
}

export interface Trimmed {
  in: number;
  out: number;
  /** The seconds the dragged edge actually moved (after the clamps). */
  delta: number;
}

/**
 * Move one edge of a clip by `delta` seconds, clamped to the source (0 to its length), the shortest clip and, on a free
 * track, the neighbouring clips. On the main track the engine closes the gap (it is magnetic), so only the source range
 * changes; on a free track moving the start edge also moves the clip's start (the content stays where it is in time).
 */
export function trimClip(clip: ClipOut, edge: Edge, delta: number, limits: TrimLimits): Trimmed {
  if (edge === 'start') {
    const earliest = Math.max(
      0, // the engine never takes an in point below the start of the source (a title's "source" starts at 0 too)
      limits.leftLimit === null
        ? Number.NEGATIVE_INFINITY
        : clip.in_point - (clip.start - limits.leftLimit),
    );
    const next = Math.min(Math.max(clip.in_point + delta, earliest), clip.out_point - MIN_CLIP);
    return { in: ms(next), out: clip.out_point, delta: ms(next - clip.in_point) };
  }
  const latest = Math.min(
    limits.sourceDuration ?? Number.POSITIVE_INFINITY,
    limits.rightLimit === null
      ? Number.POSITIVE_INFINITY
      : clip.out_point + (limits.rightLimit - clip.end),
  );
  const next = Math.max(Math.min(clip.out_point + delta, latest), clip.in_point + MIN_CLIP);
  return { in: clip.in_point, out: ms(next), delta: ms(next - clip.out_point) };
}

/** The `trim_clip` op for a trim, or null when nothing changed. */
export function trimOps(clip: ClipOut, edge: Edge, trimmed: Trimmed): EditOp[] {
  if (Math.abs(trimmed.delta) < 0.0005) return [];
  return [
    edge === 'start'
      ? { op: 'trim_clip', clip: clip.id, in: trimmed.in }
      : { op: 'trim_clip', clip: clip.id, out: trimmed.out },
  ];
}

/** The neighbouring edges of a clip on a free track (null on the main track, where nothing can be run into). */
export function neighbourLimits(
  lane: Lane,
  clip: ClipOut,
): Pick<TrimLimits, 'leftLimit' | 'rightLimit'> {
  if (lane.track.magnetic) return { leftLimit: null, rightLimit: null };
  const others = lane.clips.filter((c) => c.id !== clip.id);
  const before = others.filter((c) => c.end <= clip.start + 1e-6).map((c) => c.end);
  const after = others.filter((c) => c.start >= clip.end - 1e-6).map((c) => c.start);
  return {
    leftLimit: before.length ? Math.max(...before) : 0,
    rightLimit: after.length ? Math.min(...after) : null,
  };
}

export function trimLimits(lane: Lane, clip: ClipOut): TrimLimits {
  return {
    sourceDuration: clip.kind === 'text' ? null : clip.source_duration,
    ...neighbourLimits(lane, clip),
  };
}

// ---------------------------------------------------------------------------------------------------------
// Move

/**
 * Where a dragged main-track clip lands: the number of the other clips whose middle is before the dragged clip's
 * middle. That is the `to_index` of `move_clip`.
 */
export function reorderIndex(clips: readonly ClipOut[], dragged: number, delta: number): number {
  const clip = clips[dragged];
  if (!clip) return dragged;
  const middle = clip.start + clip.duration / 2 + delta;
  return clips.filter((c, i) => i !== dragged && c.start + c.duration / 2 < middle).length;
}

/** The timeline time of the gap a reordered clip would drop into (for the drop marker). */
export function reorderMarker(clips: readonly ClipOut[], dragged: number, toIndex: number): number {
  const others = clips.filter((_, i) => i !== dragged);
  const before = others[toIndex - 1];
  const after = others[toIndex];
  if (before) return before.end;
  return after ? after.start : 0;
}

export function reorderOps(clips: readonly ClipOut[], dragged: number, toIndex: number): EditOp[] {
  const clip = clips[dragged];
  if (!clip || toIndex === dragged) return [];
  return [{ op: 'move_clip', clip: clip.id, to_index: toIndex }];
}

/** A clip on a free track moved by `delta` seconds, optionally to another lane (`track`): the `place_clip` op. */
export function placeOps(
  clip: ClipOut,
  ownTrackId: string,
  delta: number,
  targetTrackId: string,
): EditOp[] {
  const start = Math.max(0, ms(clip.start + delta));
  if (Math.abs(start - clip.start) < 0.0005 && targetTrackId === ownTrackId) return [];
  return [
    {
      op: 'place_clip',
      clip: clip.id,
      start,
      ...(targetTrackId === ownTrackId ? {} : { track: targetTrackId }),
    },
  ];
}

// ---------------------------------------------------------------------------------------------------------
// Keyboard steps

/** The keyboard step for moving and trimming: 0.1 s when zoomed in, a second when zoomed far out. */
export function keyboardStep(pxPerSecond: number): number {
  return pxPerSecond < 5 ? 1 : 0.1;
}

// ---------------------------------------------------------------------------------------------------------
// Dropping a Media card

let counter = 0;
/** A track id the engine accepts (letters, digits, `_`, `-`, `.`; at most 64) for a track a drop creates. */
export function newTrackId(prefix: string): string {
  counter += 1;
  return `${prefix}-${Date.now().toString(36)}${counter}`;
}

/**
 * The ops that add `asset` where it was dropped: at `time` on the lane under the pointer. The main track inserts at the
 * drop position (clips stay back to back); other tracks place at `time`; an audio-only file always goes on an audio
 * track, a new one if there is none.
 */
export function dropOps(
  asset: Pick<AssetOut, 'id' | 'has_video'>,
  lane: Lane | null,
  time: number,
  lanes: readonly Lane[],
  mainClips: readonly ClipOut[],
): EditOp[] {
  const start = Math.max(0, ms(time));
  const audioLane = lanes.find((l) => l.kind === 'music');
  if (!asset.has_video) {
    const target = lane?.kind === 'music' ? lane : audioLane;
    if (target && !target.track.locked) {
      return [{ op: 'add_clip', asset: asset.id, track: target.track.id, start }];
    }
    const id = newTrackId('music');
    return [
      { op: 'add_track', id, kind: 'audio' },
      { op: 'add_clip', asset: asset.id, track: id, start },
    ];
  }
  if (lane && !lane.main) {
    return [{ op: 'add_clip', asset: asset.id, track: lane.track.id, start }];
  }
  const index = mainClips.filter((c) => c.start + c.duration / 2 < time).length;
  return [{ op: 'add_clip', asset: asset.id, index }];
}
