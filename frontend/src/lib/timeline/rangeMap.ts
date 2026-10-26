/**
 * Where a range of a recording (seconds of the source, as the Highlights tab has it) is on the timeline. The timeline
 * shows clips: a clip of the same asset covers the source seconds `in_point..out_point` at `start..end`, so a range is
 * on the timeline wherever it overlaps such a clip, and the engine's rules (cuts, reordering) stay in the clips.
 */
export interface MappableClip {
  asset: string;
  in_point: number;
  out_point: number;
  start: number;
}

export interface TimelineSpan {
  start: number;
  end: number;
}

/** The pieces of the source range `from..to` of `assetId` that the clips show, in timeline seconds, in time order. */
export function sourceToTimeline(
  clips: readonly MappableClip[],
  assetId: string,
  from: number,
  to: number,
): TimelineSpan[] {
  const spans: TimelineSpan[] = [];
  for (const clip of clips) {
    if (clip.asset !== assetId) continue;
    const lo = Math.max(from, clip.in_point);
    const hi = Math.min(to, clip.out_point);
    if (hi <= lo) continue;
    spans.push({
      start: clip.start + (lo - clip.in_point),
      end: clip.start + (hi - clip.in_point),
    });
  }
  return spans.sort((a, b) => a.start - b.start);
}
