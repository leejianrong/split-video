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
  function toggleDetail() {
    const next = timeline.getClassificationMode() === "coarse" ? "detail" : "coarse";
    timeline.setClassificationMode(next);
    detailToggleBtn.textContent = next === "coarse" ? "Show detail lanes" : "Show coarse view";
  }

  function reveal(data) {
    timeline.setClassification(data);
    toolbarEl.classList.remove("hidden");
  }

  async function poll(jobId) {
    let status;
    do {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      status = await api.getAnalysisStatus(jobId);
      if (status.total > 0) {
        analyzeBtn.textContent = `Analyzing… ${Math.round((status.completed / status.total) * 100)}%`;
      }
    } while (status.status === "running");
    return status;
  }

  async function run() {
    analyzeBtn.disabled = true;
    analyzeBtn.textContent = "Analyzing…";
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
      analyzeBtn.textContent = "Analyze audio";
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
}
