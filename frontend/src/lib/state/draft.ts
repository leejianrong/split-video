import type { ProjectOut } from '../api/client';
import { mainTrack } from '../timeline/lanes';
import type { DraftSource } from './playback.svelte';

/**
 * The main track's own file at `time`, for the draft that plays while a composite chunk renders: the clip under the
 * playhead, read straight from its asset (`/api/v1/media/asset/{id}`, which supports Range). No overlays, titles or other
 * tracks; null where the main track has no clip.
 */
export function draftFor(timeline: ProjectOut | null, time: number): DraftSource | null {
  const clip = mainTrack(timeline)?.clips.find((c) => c.start <= time && time < c.end);
  if (!clip) return null;
  return {
    url: `/api/v1/media/asset/${encodeURIComponent(clip.asset)}`,
    at: clip.in_point + (time - clip.start),
    until: clip.end,
    toTimeline: (source) => clip.start + (source - clip.in_point),
  };
}
