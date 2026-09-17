// In-memory application state. No framework: this module is the single
// source of truth, mutated by small pure-ish helpers and read directly by
// timeline.js / controls.js / exportModal.js / main.js.
//
// The editable model is a flat sorted array of internal split-point times
// (`splitPoints`), bounded by a read-only `firstStart`/`lastEnd` span (the
// silence-trimmed edges of the whole recording, as last computed by the
// server). Segments are always derived from that span + those points,
// rather than being their own independently-edited list — this keeps
// "add/drag/delete a split" and "replace everything via recompute" from
// ever getting out of sync with each other.

export const state = {
  filename: "",
  duration: 0,
  videoUrl: "",
  params: {
    silence_threshold: -35,
    min_silence_duration: 2.0,
    min_song_length: 30.0,
    padding: 0.15,
  },
  silences: [], // raw intervals from the last /api/detect call
  firstStart: 0,
  lastEnd: 0,
  splitPoints: [], // sorted, strictly between firstStart and lastEnd
  // Per-segment editor metadata (#19 label/color, #18 include/export name),
  // one entry per current segment, positionally aligned with
  // derivedSegments() — segments have no identity of their own (see above),
  // so a split/merge has to explicitly carry this along; see
  // addSplitPoint/removeSplitPointAt below.
  segmentMeta: [],
};

function defaultMeta() {
  return { label: "", color: null, included: true, exportName: null };
}

// Minimum gap kept between any two boundaries (including firstStart/lastEnd)
// so a drag/add can never produce a zero- or negative-length segment. This
// is deliberately much smaller than min_song_length: that's an
// auto-detection concept the user is explicitly overriding by hand here.
const MIN_GAP = 0.05;

export function loadSegments(segments) {
  if (!segments || segments.length === 0) {
    state.firstStart = 0;
    state.lastEnd = state.duration;
    state.splitPoints = [];
    state.segmentMeta = [];
    return;
  }
  state.firstStart = segments[0].start;
  state.lastEnd = segments[segments.length - 1].end;
  state.splitPoints = segments.slice(0, -1).map((s) => s.end);
  state.segmentMeta = segments.map((s) => ({
    label: s.label || "",
    color: s.color || null,
    included: s.included !== false,
    exportName: s.export_name || null,
  }));
}

export function derivedSegments() {
  const bounds = [state.firstStart, ...state.splitPoints, state.lastEnd];
  const segments = [];
  for (let i = 0; i < bounds.length - 1; i++) {
    const meta = state.segmentMeta[i] || defaultMeta();
    segments.push({
      index: i + 1,
      start: bounds[i],
      end: bounds[i + 1],
      duration: bounds[i + 1] - bounds[i],
      label: meta.label,
      color: meta.color,
      included: meta.included,
      exportName: meta.exportName,
    });
  }
  return segments;
}

/** Update one segment's editor metadata (label/color/included/exportName —
 * only the given keys are changed) by its current position. */
export function updateSegmentMeta(index, patch) {
  const current = state.segmentMeta[index] || defaultMeta();
  state.segmentMeta[index] = { ...current, ...patch };
}

/** Whether a split point could be placed at time `t` (far enough from
 * firstStart/lastEnd and from every existing split point). */
export function canAddSplitPoint(t) {
  if (t <= state.firstStart + MIN_GAP || t >= state.lastEnd - MIN_GAP) return false;
  for (const p of state.splitPoints) {
    if (Math.abs(p - t) < MIN_GAP) return false;
  }
  return true;
}

/** Insert a new split point at time `t`. Returns its index, or null if `t`
 * is too close to an existing boundary to place one there. */
export function addSplitPoint(t) {
  if (!canAddSplitPoint(t)) return null;
  // The segment this new point falls inside (0-based) is about to become
  // two segments — carry its label/color/included forward to both halves
  // rather than losing them; a custom export name doesn't clearly apply to
  // either half of a merged block, so that alone resets to default.
  const segmentIndex = state.splitPoints.filter((sp) => sp < t).length;
  const inherited = { ...(state.segmentMeta[segmentIndex] || defaultMeta()), exportName: null };
  state.segmentMeta[segmentIndex] = inherited;
  state.segmentMeta.splice(segmentIndex, 0, { ...inherited });

  state.splitPoints.push(t);
  state.splitPoints.sort((a, b) => a - b);
  return state.splitPoints.indexOf(t);
}

export function removeSplitPointAt(index) {
  state.splitPoints.splice(index, 1);
  // Removing the boundary between segment `index` and `index + 1` merges
  // them — keep the earlier one's metadata, drop the later one's.
  state.segmentMeta.splice(index + 1, 1);
}

/** Move the split point at `index` to `newT`, clamped so it can never cross
 * its neighbors (or firstStart/lastEnd at the ends). Returns the clamped
 * value actually applied. */
export function moveSplitPointAt(index, newT) {
  const lower = index > 0 ? state.splitPoints[index - 1] : state.firstStart;
  const upper = index < state.splitPoints.length - 1 ? state.splitPoints[index + 1] : state.lastEnd;
  const clamped = Math.min(Math.max(newT, lower + MIN_GAP), upper - MIN_GAP);
  state.splitPoints[index] = clamped;
  return clamped;
}

export function formatTime(seconds, totalDuration) {
  const whole = Math.round(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  if (totalDuration >= 3600) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}
