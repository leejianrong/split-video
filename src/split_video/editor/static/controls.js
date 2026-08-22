// The 4 threshold sliders. Three are cheap (pure Python recompute, no
// ffmpeg) and recompute live on a short debounce; the fourth
// (silence-threshold) requires a real ffmpeg pass, so it only updates its
// own readout live and requires clicking "Recompute" to actually take
// effect. Both paths fully replace the current segment list, per the
// product decision that any recompute discards prior manual edits.

import { state } from "./state.js";
import * as api from "./api.js";

const CHEAP_DEBOUNCE_MS = 150;

export function createControls({ elements, onSegmentsReplaced }) {
  const {
    thresholdInput,
    thresholdValue,
    minSilenceInput,
    minSilenceValue,
    minSongInput,
    minSongValue,
    paddingInput,
    paddingValue,
    recomputeBtn,
    panelEls,
  } = elements;

  let debounceTimer = null;
  let thresholdDirty = false;

  function syncReadouts() {
    thresholdValue.textContent = `${thresholdInput.value} dB`;
    minSilenceValue.textContent = `${parseFloat(minSilenceInput.value).toFixed(1)} s`;
    minSongValue.textContent = `${parseFloat(minSongInput.value).toFixed(0)} s`;
    paddingValue.textContent = `${parseFloat(paddingInput.value).toFixed(2)} s`;
  }

  function setBusy(busy) {
    for (const el of panelEls) el.disabled = busy;
    if (busy) {
      recomputeBtn.textContent = "Detecting…";
    } else {
      recomputeBtn.textContent = thresholdDirty ? "Recompute*" : "Recompute";
      recomputeBtn.classList.toggle("dirty", thresholdDirty);
    }
  }

  function cheapParams() {
    return {
      silences: state.silences,
      duration: state.duration,
      minSilenceDuration: parseFloat(minSilenceInput.value),
      minSongLength: parseFloat(minSongInput.value),
      padding: parseFloat(paddingInput.value),
    };
  }

  async function recomputeCheap() {
    let res;
    try {
      res = await api.computeSegments(cheapParams());
    } catch (err) {
      console.error(err);
      return;
    }
    if (res === null) return; // superseded by a newer request
    onSegmentsReplaced(res.segments);
  }

  function scheduleCheapRecompute() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(recomputeCheap, CHEAP_DEBOUNCE_MS);
  }

  [minSilenceInput, minSongInput, paddingInput].forEach((el) => {
    el.addEventListener("input", () => {
      syncReadouts();
      scheduleCheapRecompute();
    });
  });

  thresholdInput.addEventListener("input", () => {
    syncReadouts();
    thresholdDirty = true;
    recomputeBtn.textContent = "Recompute*";
    recomputeBtn.classList.add("dirty");
  });

  recomputeBtn.addEventListener("click", async () => {
    setBusy(true);
    try {
      const detectRes = await api.detectSilence(parseFloat(thresholdInput.value));
      state.silences = detectRes.silences;
      const segRes = await api.computeSegments(cheapParams());
      if (segRes !== null) {
        onSegmentsReplaced(segRes.segments);
        thresholdDirty = false;
      }
    } catch (err) {
      alert(`Recompute failed: ${err.message}`);
    } finally {
      setBusy(false);
    }
  });

  function initFromState() {
    thresholdInput.value = state.params.silence_threshold;
    minSilenceInput.value = state.params.min_silence_duration;
    minSongInput.value = state.params.min_song_length;
    paddingInput.value = state.params.padding;
    syncReadouts();
  }

  return { initFromState };
}
