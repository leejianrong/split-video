// The first-run modal: choose whether to detect splits by silence and/or
// kick off YAMNet analysis before starting to edit a freshly-opened video.
// Shown once per video — main.js only calls open() when #14's `resumed`
// flag is false — and never again once a project file exists, since
// finishing this (by either button) saves one immediately.
//
// The silence-detection half reuses controls.js as-is: it's already a
// self-contained "sliders in, recomputed segment list out" module with no
// assumption baked in about where those sliders live.

import { derivedSegments, loadSegments } from "./state.js";
import * as api from "./api.js";
import { createControls } from "./controls.js";

export function createOnboarding({ modalEl, overlayEl, elements }) {
  const { analyzeCheckbox, silenceCheckbox, silencePanel, previewCountEl, skipBtn, startBtn, controlsElements } =
    elements;

  let previewSegments = [];

  const controls = createControls({
    elements: controlsElements,
    onSegmentsReplaced: (segments) => {
      previewSegments = segments;
      previewCountEl.textContent =
        segments.length > 0 ? `This would give you ${segments.length} segment${segments.length === 1 ? "" : "s"}.` : "";
    },
  });

  function updateSilenceVisibility() {
    silencePanel.classList.toggle("hidden", !silenceCheckbox.checked);
  }
  silenceCheckbox.addEventListener("change", updateSilenceVisibility);

  /** Shows the modal and resolves once the user finishes setup (by either
   * button), with the segments to start editing from and whether analysis
   * should kick off. Never rejects — a save failure alerts and lets the
   * user retry rather than leaving the caller in an unresolved state. */
  function open() {
    controls.initFromState();
    updateSilenceVisibility();
    previewCountEl.textContent = "";
    modalEl.classList.remove("hidden");
    overlayEl.classList.remove("hidden");

    return new Promise((resolve) => {
      function cleanup() {
        modalEl.classList.add("hidden");
        overlayEl.classList.add("hidden");
        startBtn.removeEventListener("click", onStart);
        skipBtn.removeEventListener("click", onSkip);
      }

      async function commit(skip) {
        startBtn.disabled = true;
        skipBtn.disabled = true;
        try {
          const chosen = !skip && silenceCheckbox.checked ? previewSegments : [];
          loadSegments(chosen);
          const toSave = derivedSegments();
          await api.saveProject(toSave);
          cleanup();
          resolve({ segments: toSave, runAnalysis: !skip && analyzeCheckbox.checked });
        } catch (err) {
          alert(`Couldn't finish setup: ${err.message}`);
          startBtn.disabled = false;
          skipBtn.disabled = false;
        }
      }

      function onStart() {
        commit(false);
      }
      function onSkip() {
        commit(true);
      }

      startBtn.addEventListener("click", onStart);
      skipBtn.addEventListener("click", onSkip);
    });
  }

  return { open };
}
