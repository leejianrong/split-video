// Bootstrap: fetch initial state, wire up the player/timeline/controls/export
// modal, render.

import { state, loadSegments, derivedSegments } from "./state.js";
import * as api from "./api.js";
import { createPlayer } from "./player.js";
import { createTimeline } from "./timeline.js";
import { createExportModal } from "./exportModal.js";
import { createFilePicker } from "./filePicker.js";
import { createAnalysisControl } from "./analysis.js";
import { createSegmentTable } from "./segmentTable.js";
import { createShortcuts } from "./shortcuts.js";
import { createOnboarding } from "./onboarding.js";

function updateHeader() {
  document.getElementById("filename").textContent = state.filename;
  const count = derivedSegments().length;
  document.getElementById("segment-count").textContent = `${count} segment${count === 1 ? "" : "s"}`;
}

// Persists the current splits to the sidecar project file (see #14) so
// they survive closing and reopening the editor. Every add/move/delete/
// recompute is already a discrete, infrequent user action — no debouncing
// needed on top of that.
function createSaveIndicator() {
  const el = document.getElementById("save-status");
  let saveCount = 0;

  async function save() {
    const thisSave = ++saveCount;
    el.classList.remove("error");
    el.textContent = "Saving…";
    try {
      await api.saveProject(derivedSegments());
      if (thisSave === saveCount) el.textContent = "Saved";
    } catch (err) {
      if (thisSave === saveCount) {
        el.classList.add("error");
        el.textContent = `Save failed: ${err.message}`;
      }
    }
  }

  return { save };
}

async function main() {
  const session = await api.getSession();
  const filePicker = document.getElementById("file-picker");
  const appMain = document.getElementById("app-main");

  if (!session.file_open) {
    filePicker.classList.remove("hidden");
    appMain.classList.add("hidden");
    document.getElementById("filename").textContent = "Choose a video to edit";
    const picker = createFilePicker({
      listEl: document.getElementById("browse-list"),
      pathEl: document.getElementById("browse-path"),
      upBtn: document.getElementById("browse-up"),
      statusEl: document.getElementById("browse-status"),
      statusTextEl: document.getElementById("browse-status-text"),
      onOpen: async () => {
        filePicker.classList.add("hidden");
        appMain.classList.remove("hidden");
        try {
          await bootEditor();
        } catch (err) {
          reportBootFailure(err);
        }
      },
    });
    await picker.load("");
    return;
  }

  filePicker.classList.add("hidden");
  appMain.classList.remove("hidden");
  await bootEditor();
}

function controlsElementsForOnboarding() {
  return {
    thresholdInput: document.getElementById("silence-threshold"),
    thresholdValue: document.getElementById("silence-threshold-value"),
    minSilenceInput: document.getElementById("min-silence-duration"),
    minSilenceValue: document.getElementById("min-silence-duration-value"),
    minSongInput: document.getElementById("min-song-length"),
    minSongValue: document.getElementById("min-song-length-value"),
    paddingInput: document.getElementById("silence-padding"),
    paddingValue: document.getElementById("silence-padding-value"),
    recomputeBtn: document.getElementById("recompute-btn"),
    panelEls: [
      document.getElementById("silence-threshold"),
      document.getElementById("min-silence-duration"),
      document.getElementById("min-song-length"),
      document.getElementById("silence-padding"),
      document.getElementById("recompute-btn"),
    ],
  };
}

async function bootEditor() {
  const data = await api.getState();
  state.filename = data.filename;
  state.duration = data.duration;
  state.videoUrl = data.video_url;
  state.params = data.params;
  state.silences = data.silences;

  // A never-saved video gets the one-time setup modal instead of any
  // automatic detection (see #16/#19's follow-up) — nothing proposes
  // splits until the user asks for it, here or later from the toolbar.
  let initialSegments = data.segments;
  let runAnalysisOnStart = false;
  if (!data.resumed) {
    const onboarding = createOnboarding({
      modalEl: document.getElementById("onboarding-modal"),
      overlayEl: document.getElementById("app-overlay"),
      elements: {
        analyzeCheckbox: document.getElementById("onboarding-analyze"),
        silenceCheckbox: document.getElementById("onboarding-silence"),
        silencePanel: document.getElementById("onboarding-silence-panel"),
        previewCountEl: document.getElementById("onboarding-preview-count"),
        skipBtn: document.getElementById("onboarding-skip"),
        startBtn: document.getElementById("onboarding-start"),
        controlsElements: controlsElementsForOnboarding(),
      },
    });
    const result = await onboarding.open();
    initialSegments = result.segments;
    runAnalysisOnStart = result.runAnalysis;
  }

  loadSegments(initialSegments);
  updateHeader();

  const saveIndicator = createSaveIndicator();
  if (data.resumed) document.getElementById("save-status").textContent = "Resumed saved splits";

  const videoEl = document.getElementById("video");
  videoEl.src = state.videoUrl;
  const player = createPlayer(videoEl);

  createShortcuts({
    player,
    shortcutsBtn: document.getElementById("shortcuts-btn"),
    modalEl: document.getElementById("shortcuts-modal"),
    overlayEl: document.getElementById("app-overlay"),
  });

  // Assigned right below — referenced here only inside a callback that
  // fires later (on a user edit), by which point both exist.
  let timeline;
  let segmentTable;
  function handleSegmentsChanged() {
    updateHeader();
    timeline.render();
    segmentTable.render();
    saveIndicator.save();
  }

  timeline = createTimeline({
    viewport: document.getElementById("timeline-viewport"),
    track: document.getElementById("timeline-track"),
    ruler: document.getElementById("ruler"),
    bands: document.getElementById("bands"),
    playhead: document.getElementById("playhead"),
    waveformCanvas: document.getElementById("waveform"),
    classificationRow: document.getElementById("classification-row"),
    player,
    splitBtn: document.getElementById("split-btn"),
    deleteSplitBtn: document.getElementById("delete-split-btn"),
    onChange: handleSegmentsChanged,
  });
  timeline.fit();

  segmentTable = createSegmentTable({
    bodyEl: document.getElementById("segment-table-body"),
    countEl: document.getElementById("segment-export-count"),
    onChange: handleSegmentsChanged,
  });
  segmentTable.render();

  // Fetched separately (rather than bundled into /api/state) so opening a
  // file isn't blocked on decoding its full audio track — the waveform
  // fills in once ready.
  api
    .getWaveform()
    .then((data) => timeline.setWaveform(data.buckets))
    .catch((err) => console.error("waveform fetch failed:", err));

  const analysisControl = createAnalysisControl({
    analyzeBtn: document.getElementById("analyze-btn"),
    toolbarEl: document.getElementById("classification-toolbar"),
    detailToggleBtn: document.getElementById("classification-detail-toggle"),
    timeline,
  });
  document.getElementById("analyze-btn").disabled = false;
  if (runAnalysisOnStart) analysisControl.run();

  document.getElementById("zoom-in").addEventListener("click", () => timeline.setZoom(timeline.getPxPerSec() * 1.4));
  document.getElementById("zoom-out").addEventListener("click", () => timeline.setZoom(timeline.getPxPerSec() / 1.4));
  document.getElementById("zoom-fit").addEventListener("click", () => timeline.fit());

  createExportModal({
    modalEl: document.getElementById("export-modal"),
    overlayEl: document.getElementById("app-overlay"),
    openBtn: document.getElementById("export-btn"),
  });

  document.getElementById("export-btn").disabled = false;
}

function reportBootFailure(err) {
  console.error(err);
  document.getElementById("filename").textContent = "Failed to load";
  alert(`Failed to load editor: ${err.message}`);
}

main().catch(reportBootFailure);
