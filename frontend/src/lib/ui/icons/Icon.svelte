<script lang="ts" module>
  // Our own icons: 24 x 24 stroke paths (round caps, 2 px). A starter set; add a name here when a feature needs one.
  export const ICONS = {
    play: 'M7 4.5v15l12-7.5z',
    pause: 'M8 5v14M16 5v14',
    scissors:
      'M6 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12',
    undo: 'M9 14 4 9l5-5M4 9h10.5a5.5 5.5 0 0 1 0 11H11',
    redo: 'm15 14 5-5-5-5M20 9H9.5a5.5 5.5 0 0 0 0 11H13',
    trash: 'M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 10v6M14 10v6',
    plus: 'M12 5v14M5 12h14',
    minus: 'M5 12h14',
    close: 'M6 6l12 12M18 6 6 18',
    check: 'm5 12.5 4.5 4.5L19 7.5',
    info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v6M12 7.5v.01',
    alert: 'M12 3 2 20h20zM12 10v5M12 17.5v.01',
    help: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 .9-1 1.7M12 17v.01',
    keyboard: 'M3 6h18v12H3zM7 10h.01M11 10h.01M15 10h.01M7 14h10',
    media: 'M5 5h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zM10 9l5 3-5 3z',
    highlights: 'M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.4 6.7 19.4l1.2-6L3.4 9.3l6-.7z',
    transcript: 'M4 6h16M4 11h16M4 16h10',
    text: 'M5 6h14M12 6v13',
    captions:
      'M5 5h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zM8 11h3M13 11h3M8 15h8',
    audio:
      'M9 18V6l10-2v12M6.5 20a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM16.5 18a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z',
    effects: 'M4 20 15 9M14 4v3M19 9h-3M18 3l1 2M5 8l2 1',
    folder: 'M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
    video:
      'M4 6h11a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2zM17 10l5-3v10l-5-3',
    arrowUp: 'M12 19V5M6 11l6-6 6 6',
    skipBack: 'M6 5v14M20 5v14L9 12z',
    skipForward: 'M18 5v14M4 5v14l11-7z',
    chevronDown: 'm6 9 6 6 6-6',
    upload: 'M12 16V4M7 9l5-5 5 5M4 16v4h16v-4',
    lock: 'M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5zM12 15v3',
    unlock: 'M7 11V8a5 5 0 0 1 9.5-2.2M5 11h14v10H5zM12 15v3',
    eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
    eyeOff:
      'M3 3l18 18M10.6 6.1A9.6 9.6 0 0 1 12 5c6 0 10 7 10 7a17 17 0 0 1-3.2 4M6.5 6.9A17 17 0 0 0 2 12s4 7 10 7a9.5 9.5 0 0 0 4-.9M9.9 9.9a3 3 0 0 0 4.2 4.2',
    volume: 'M4 9v6h4l5 4V5L8 9zM16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12',
    volumeOff: 'M4 9v6h4l5 4V5L8 9zM17 9l5 6M22 9l-5 6',
    fit: 'M4 12h16M8 8l-4 4 4 4M16 8l4 4-4 4M4 5v14M20 5v14',
  } as const;
  export type IconName = keyof typeof ICONS;
</script>

<script lang="ts">
  let { name, size = 18 }: { name: IconName; size?: number } = $props();
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="2"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
  focusable="false"
>
  <path d={ICONS[name]} />
</svg>

<style>
  svg {
    flex: none;
    display: block;
  }
</style>
