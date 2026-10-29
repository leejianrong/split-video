import type { ClipOut, ProjectOut } from '../api/client';
import { baseName } from '../format';
import { mainTrack } from '../timeline/lanes';

/** What the dialog offers (ux-spec #export). `shorts` is the "highlights" mode of the engine, built from labelled clips. */
export type ExportMode = 'segments' | 'timeline' | 'shorts';

export const MODES: readonly { id: ExportMode; label: string; hint: string }[] = [
  {
    id: 'segments',
    label: 'Each segment as its own file',
    hint: 'One file per segment of the main track.',
  },
  {
    id: 'timeline',
    label: 'The whole timeline as one video',
    hint: 'One file, named after the recording.',
  },
  {
    id: 'shorts',
    label: 'Each highlight as a short',
    hint: 'One vertical clip per highlight you made.',
  },
];

/** `original` is a stream copy (no re-encode): the fastest way to cut a recording into files. */
export type PresetId = 'original' | 'youtube-1080p' | 'vertical-1080' | 'square-1080' | 'draft-720';

export const PRESETS: readonly { id: PresetId; label: string; size: string }[] = [
  { id: 'youtube-1080p', label: 'YouTube 1080p', size: '1920 x 1080' },
  { id: 'vertical-1080', label: 'Vertical 1080x1920', size: '1080 x 1920' },
  { id: 'square-1080', label: 'Square 1080', size: '1080 x 1080' },
  { id: 'draft-720', label: 'Draft 720p', size: '1280 x 720' },
  { id: 'original', label: 'Original quality, fastest (no re-encode)', size: 'as recorded' },
];

export type Fit = 'crop' | 'letterbox';

export interface ExportSettings {
  mode: ExportMode;
  preset: PresetId;
  fit: Fit;
  captions: boolean;
  /** A folder in the library; '' means the default next to the recording. */
  folder: string;
  overwrite: boolean;
}

export const DEFAULTS: ExportSettings = {
  mode: 'segments',
  preset: 'youtube-1080p',
  fit: 'letterbox',
  captions: false,
  folder: '',
  overwrite: false,
};

/** One file of the plan: the main track's clip number (1-based, as the engine's `only`) and its length. */
export interface PlannedFile {
  number: number;
  label: string;
  seconds: number;
}

/** The main track's media clips in order: the segments the engine numbers. */
function segmentsOf(timeline: ProjectOut | null): { clip: ClipOut; number: number }[] {
  const main = mainTrack(timeline);
  if (!main) return [];
  const media = main.clips.filter((c) => c.kind === 'media');
  return media.map((clip, i) => ({ clip, number: i + 1 }));
}

const length = (c: ClipOut): number => Math.max(0, c.out_point - c.in_point);

/** Labels the Highlights tab gives the clips it makes (docs/highlights.md): `Short: <title>` and `Highlight N`. */
export const isHighlightLabel = (label: string | undefined): boolean =>
  !!label && (label.startsWith('Short: ') || label.startsWith('Highlight '));

/** The files a mode would write from the open timeline (the dialog's N). */
export function plannedFiles(timeline: ProjectOut | null, mode: ExportMode): PlannedFile[] {
  const all = segmentsOf(timeline);
  const pick =
    mode === 'segments'
      ? all.filter(({ clip }) => clip.included !== false && !isHighlightLabel(clip.label))
      : mode === 'shorts'
        ? all.filter(({ clip }) => isHighlightLabel(clip.label))
        : all;
  if (mode === 'timeline') {
    return all.length
      ? [{ number: 0, label: 'whole timeline', seconds: timeline?.duration ?? 0 }]
      : [];
  }
  return pick.map(({ clip, number }) => ({
    number,
    label: clip.label || `Segment ${number}`,
    seconds: length(clip),
  }));
}

/** The folder shown as the default: `<recording>_export` beside the recording. */
export function defaultFolder(videoId: string): string {
  const slash = videoId.lastIndexOf('/');
  const dir = slash >= 0 ? videoId.slice(0, slash + 1) : '';
  const name = baseName(videoId);
  const dot = name.lastIndexOf('.');
  const stem = dot > 0 ? name.slice(0, dot) : name;
  return `${dir}${stem}_export`;
}

/** The body of `POST /compose/export` for these settings, or the reason there is nothing to send. */
export function buildRequest(
  videoId: string,
  settings: ExportSettings,
  files: readonly PlannedFile[],
  only: readonly number[] | null = null,
): Record<string, unknown> {
  const folder = settings.folder.trim() || defaultFolder(videoId);
  const base: Record<string, unknown> = {
    video: videoId,
    output_dir: folder,
    overwrite: settings.overwrite,
  };
  const shorts = settings.mode === 'shorts';
  const preset = shorts ? 'vertical-1080' : settings.preset;
  if (preset !== 'original') {
    base.preset = preset;
    base.fit = shorts ? 'crop' : settings.fit;
  }
  if (settings.captions && preset !== 'original') base.captions = 'shorts';
  if (settings.mode === 'timeline') return { ...base, mode: 'timeline' };
  const numbers = only ?? files.map((f) => f.number);
  return { ...base, mode: 'segments', only: [...numbers] };
}

/** Rough seconds of work per second of output, by preset (x264 `veryfast` on a few cores; measured loosely). */
const SPEED: Record<PresetId, number> = {
  original: 0.01,
  'draft-720': 0.12,
  'youtube-1080p': 0.35,
  'square-1080': 0.3,
  'vertical-1080': 0.3,
};

/** Estimated wall time in seconds for `files`, `concurrency` at once. Burned-in captions add a little. */
export function estimateSeconds(
  files: readonly PlannedFile[],
  settings: ExportSettings,
  concurrency = 2,
): number {
  const preset: PresetId = settings.mode === 'shorts' ? 'vertical-1080' : settings.preset;
  const work = files.reduce((sum, f) => sum + f.seconds, 0) * SPEED[preset];
  const extra = settings.captions && preset !== 'original' ? 1.15 : 1;
  const longest = Math.max(0, ...files.map((f) => f.seconds * SPEED[preset]));
  return Math.max(longest, (work * extra) / Math.max(1, concurrency));
}

export function formatEstimate(seconds: number): string {
  if (seconds < 20) return 'under a minute';
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return 'under a minute';
  if (minutes < 90) return `about ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return `about ${hours} h${rest ? ` ${rest} min` : ''}`;
}
