// "Analyze audio" button: kicks off the background YAMNet classification
// job, polls its progress into the button's own label (mirroring
// controls.js's "Detecting…" pattern for the silence-threshold recompute),
// then hands the resulting regions/lanes to the timeline. Also checks on
// boot whether the file was already analyzed (e.g. after a page reload) and
// restores that state without re-running anything. The coarse/detail toggle
// lives here too since it only makes sense once analysis has produced
// something to toggle the view of.

import * as api from "./api.js";

const POLL_INTERVAL_MS = 400;

export function createAnalysisControl({ analyzeBtn, toolbarEl, detailToggleBtn, timeline }) {
  // The button's own text lives in this child <span> — analyzeBtn also
  // holds an icon <svg>, so writing analyzeBtn.textContent directly would
  // silently delete it.
  const analyzeLabel = document.getElementById("analyze-btn-label");

  function updateDetailToggle() {
    const mode = timeline.getClassificationMode();
    detailToggleBtn.classList.toggle("active", mode === "detail");
    detailToggleBtn.title =
      mode === "detail" ? "Collapse to one row" : "Show every overlapping label in its own lane";
  }

  function toggleDetail() {
    timeline.setClassificationMode(timeline.getClassificationMode() === "coarse" ? "detail" : "coarse");
    updateDetailToggle();
  }

  function reveal(data) {
    timeline.setClassification(data);
    toolbarEl.classList.remove("hidden");
    detailToggleBtn.disabled = false;
    updateDetailToggle();
    // A click from here on is a cheap no-op (the backend already has a
    // result, cached this session or loaded from disk) — reword the
    // button so that's clear rather than implying more work is needed.
    analyzeLabel.textContent = "Re-analyze audio";
  }

  // Checks status immediately rather than always sleeping first, so an
  // already-analyzed source (the backend reports a job as instantly
  // "done" — see analyze() in app.py) doesn't sit at "Analyzing…" for a
  // full poll interval before resolving.
  async function poll(jobId) {
    let status = await api.getAnalysisStatus(jobId);
    while (status.status === "running") {
      if (status.total > 0) {
        analyzeLabel.textContent = `Analyzing… ${Math.round((status.completed / status.total) * 100)}%`;
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      status = await api.getAnalysisStatus(jobId);
    }
    return status;
  }

  async function run() {
    const wasAnalyzed = !toolbarEl.classList.contains("hidden");
    analyzeBtn.disabled = true;
    analyzeLabel.textContent = "Analyzing…";
    try {
      const { job_id } = await api.startAnalysis();
      const status = await poll(job_id);
      if (status.status === "error") {
        alert(`Audio analysis failed: ${status.error}`);
        return;
      }
      reveal(await api.getClassification());
    } catch (err) {
      alert(`Audio analysis failed: ${err.message}`);
    } finally {
      analyzeBtn.disabled = false;
      // reveal() already relabeled the button on success; only restore a
      // label here if that never happened (the error paths above).
      if (analyzeLabel.textContent.startsWith("Analyzing")) {
        analyzeLabel.textContent = wasAnalyzed ? "Re-analyze audio" : "Analyze audio";
      }
    }
  }

  analyzeBtn.addEventListener("click", run);
  detailToggleBtn.addEventListener("click", toggleDetail);

  api
    .getClassification()
    .then((data) => {
      if (data.analyzed) reveal(data);
    })
    .catch((err) => console.error("classification fetch failed:", err));

  // Exposed so the onboarding modal (see onboarding.js) can kick off
  // analysis itself when the user checks that option there, instead of
  // requiring a separate click on the toolbar button afterwards.
  return { run };
}
