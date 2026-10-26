import type { ClipOut } from '../api/client';
import {
  placeOps,
  reorderIndex,
  reorderMarker,
  reorderOps,
  trimClip,
  trimLimits,
  trimOps,
  type EditOp,
  type Edge,
} from './edits';
import { canDropOn, type Lane } from './lanes';

export type DragKind = 'move' | 'trim-start' | 'trim-end';

/** A pointer must travel this far before a press becomes a drag; below it the press is a click. */
export const DRAG_THRESHOLD_PX = 4;

/** What the lanes draw while a drag is in progress. */
export interface DragState {
  kind: DragKind;
  clipId: string;
  laneId: string;
  /** Where the clip is drawn right now, in timeline seconds. */
  start: number;
  end: number;
  /** A moving clip follows the pointer vertically too (px). */
  dy: number;
  /** The lane the clip would land on (move), if it differs from where it is. */
  targetLaneId: string | null;
  /** Main track: the time of the gap the clip would drop into. */
  marker: number | null;
  /** The operations a release commits (empty: nothing would change). */
  ops: EditOp[];
  /** The tooltip while trimming ("New length 4 min 12 s"). */
  hint: string;
  moved: boolean;
}

export interface DragDeps {
  pxPerSecond(): number;
  lanes(): readonly Lane[];
  /** The lane under a y offset inside the lanes area (px), accounting for vertical scroll. */
  laneAtY(y: number): Lane | null;
  /** Run the operations as one edit; resolves with whether they applied. */
  commit(ops: EditOp[], what: string): Promise<boolean>;
}

export type DragOutcome = 'click' | 'committed' | 'rejected' | 'nothing';

/**
 * One pointer drag on a clip: a move (in time, and across lanes) or a trim of one edge. `begin` on pointer down,
 * `update` on each (rAF-throttled) pointer move, `finish` on release. Only `finish` talks to the engine, with one
 * commit of the operations the geometry came to; `cancel` (Escape, lost capture) drops it. All maths is in `edits.ts`.
 */
export class ClipDrag {
  active = $state<DragState | null>(null);
  #deps: DragDeps;
  #origin = { x: 0, y: 0 };
  #lane: Lane | null = null;
  #clip: ClipOut | null = null;

  constructor(deps: DragDeps) {
    this.#deps = deps;
  }

  begin(kind: DragKind, lane: Lane, clip: ClipOut, x: number, y: number): void {
    this.#origin = { x, y };
    this.#lane = lane;
    this.#clip = clip;
    this.active = {
      kind,
      clipId: clip.id,
      laneId: lane.id,
      start: clip.start,
      end: clip.end,
      dy: 0,
      targetLaneId: null,
      marker: null,
      ops: [],
      hint: '',
      moved: false,
    };
  }

  update(x: number, y: number, laneY: number): void {
    const state = this.active;
    const lane = this.#lane;
    const clip = this.#clip;
    if (!state || !lane || !clip) return;
    const dxPx = x - this.#origin.x;
    const dyPx = y - this.#origin.y;
    if (!state.moved && Math.hypot(dxPx, dyPx) < DRAG_THRESHOLD_PX) return;
    const delta = dxPx / this.#deps.pxPerSecond();
    if (state.kind === 'move') this.#move(state, lane, clip, delta, dyPx, laneY);
    else this.#trim(state, lane, clip, state.kind === 'trim-start' ? 'start' : 'end', delta);
    state.moved = true;
    this.active = { ...state };
  }

  #trim(state: DragState, lane: Lane, clip: ClipOut, edge: Edge, delta: number): void {
    const trimmed = trimClip(clip, edge, delta, trimLimits(lane, clip));
    state.start = edge === 'start' ? clip.start + trimmed.delta : clip.start;
    state.end = edge === 'end' ? clip.end + trimmed.delta : clip.end;
    state.ops = trimOps(clip, edge, trimmed);
    const length = clip.duration + (edge === 'start' ? -trimmed.delta : trimmed.delta);
    state.hint = `Length ${length.toFixed(1)} s`;
  }

  #move(
    state: DragState,
    lane: Lane,
    clip: ClipOut,
    delta: number,
    dyPx: number,
    laneY: number,
  ): void {
    if (lane.track.magnetic) {
      const index = lane.clips.findIndex((c) => c.id === clip.id);
      const to = reorderIndex(lane.clips, index, delta);
      state.start = clip.start + delta;
      state.end = clip.end + delta;
      state.dy = 0;
      state.targetLaneId = null;
      state.marker = to === index ? null : reorderMarker(lane.clips, index, to);
      state.ops = reorderOps(lane.clips, index, to);
      state.hint = to === index ? '' : `Position ${to + 1} of ${lane.clips.length}`;
      return;
    }
    const target = this.#deps.laneAtY(laneY);
    const landing = target && target.id !== lane.id && canDropOn(lane, target) ? target : lane;
    const start = Math.max(0, clip.start + delta);
    state.start = start;
    state.end = start + clip.duration;
    state.dy = dyPx;
    state.targetLaneId = landing.id === lane.id ? null : landing.id;
    state.marker = null;
    state.ops = placeOps(clip, lane.track.id, start - clip.start, landing.track.id);
    state.hint = `Starts at ${start.toFixed(1)} s`;
  }

  /** Release: commit what the drag came to. A press that never travelled is a `click` (the caller selects). */
  async finish(): Promise<DragOutcome> {
    const state = this.active;
    this.active = null;
    this.#lane = this.#clip = null;
    if (!state) return 'nothing';
    if (!state.moved) return 'click';
    if (state.ops.length === 0) return 'nothing';
    const what = state.kind === 'move' ? 'Moved' : 'Trimmed';
    return (await this.#deps.commit(state.ops, what)) ? 'committed' : 'rejected';
  }

  cancel(): void {
    this.active = null;
    this.#lane = this.#clip = null;
  }
}
