import type { VideoSlot } from './playback.svelte';

const WAIT_MS = 15_000;

/** Resolve on `event`, reject on a media error or after `WAIT_MS`: a wedged load must not hang playback. */
function once(el: HTMLVideoElement, event: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const done = (error?: Error) => {
      clearTimeout(timer);
      el.removeEventListener(event, onEvent);
      el.removeEventListener('error', onError);
      if (error) reject(error);
      else resolve();
    };
    const onEvent = () => done();
    const onError = () => done(new Error(`media error ${el.error?.code ?? ''}`));
    const timer = setTimeout(() => done(new Error(`no ${event} event`)), WAIT_MS);
    el.addEventListener(event, onEvent);
    el.addEventListener('error', onError);
  });
}

/** Wrap a `<video>` element as the slot `PlaybackStore` drives. */
export function videoSlot(el: HTMLVideoElement): VideoSlot {
  return {
    load(src) {
      const ready = once(el, 'loadeddata');
      el.preload = 'auto';
      el.src = src;
      el.load();
      return ready;
    },
    seek(time) {
      if (Math.abs(el.currentTime - time) < 0.001) return Promise.resolve();
      const seeked = once(el, 'seeked');
      el.currentTime = time;
      return seeked;
    },
    play: () => el.play(),
    pause: () => el.pause(),
    clear() {
      el.pause();
      el.removeAttribute('src');
      el.load(); // releases the decoder and the buffered data
    },
    get source() {
      return el.getAttribute('src');
    },
    get time() {
      return el.currentTime;
    },
    get duration() {
      return Number.isFinite(el.duration) ? el.duration : 0;
    },
    get ended() {
      return el.ended;
    },
  };
}
