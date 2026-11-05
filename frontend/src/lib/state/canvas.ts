/** The project canvas: the output ratio chosen in the top bar. Each one is an export preset (src/split_video/presets.py). */
export type CanvasId = '16:9' | '9:16' | '1:1';

export const CANVASES: { id: CanvasId; preset: string; ratio: number }[] = [
  { id: '16:9', preset: 'youtube-1080p', ratio: 16 / 9 },
  { id: '9:16', preset: 'vertical-1080', ratio: 9 / 16 },
  { id: '1:1', preset: 'square-1080', ratio: 1 },
];

export const canvasOf = (id: CanvasId) => CANVASES.find((c) => c.id === id)!;
