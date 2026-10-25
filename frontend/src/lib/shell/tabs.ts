import type { IconName } from '../ui/icons/Icon.svelte';
import type { TabId } from '../state/layout';

/** The left tab rail, in the fixed order of docs/ux-spec.md#left-tabs. */
export const TABS: readonly { id: TabId; label: string; icon: IconName }[] = [
  { id: 'media', label: 'Media', icon: 'media' },
  { id: 'highlights', label: 'Highlights', icon: 'highlights' },
  { id: 'transcript', label: 'Transcript', icon: 'transcript' },
  { id: 'text', label: 'Text', icon: 'text' },
  { id: 'captions', label: 'Captions', icon: 'captions' },
  { id: 'audio', label: 'Audio', icon: 'audio' },
  { id: 'effects', label: 'Effects', icon: 'effects' },
];

/** What an empty panel says (docs/ux-spec.md#empty-states; the ones the spec does not word are neutral). */
export const EMPTY_COPY: Record<TabId, string> = {
  media: 'Add your first recording',
  highlights: 'No highlights yet. Run Find highlights.',
  transcript: 'Not transcribed yet.',
  text: 'No titles yet.',
  captions: 'Captions are made from the transcript.',
  audio: 'No music or sounds yet.',
  effects: 'Coming later.',
};
