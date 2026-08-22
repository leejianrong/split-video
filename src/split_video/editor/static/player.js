// Thin wrapper around the <video> element so timeline.js doesn't need to
// know about the DOM node directly.

export function createPlayer(videoEl) {
  const listeners = new Set();

  videoEl.addEventListener("timeupdate", () => {
    for (const fn of listeners) fn(videoEl.currentTime);
  });

  return {
    element: videoEl,
    seek(t) {
      videoEl.currentTime = Math.max(0, t);
    },
    play() {
      videoEl.play();
    },
    pause() {
      videoEl.pause();
    },
    get currentTime() {
      return videoEl.currentTime;
    },
    /** Subscribe to playback time updates; returns an unsubscribe function. */
    onTimeUpdate(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
