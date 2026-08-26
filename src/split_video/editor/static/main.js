// Bootstrap: fetch initial state, wire up the player/timeline/controls/export
// modal, render.

import { state, loadSegments, derivedSegments } from "./state.js";
import * as api from "./api.js";
import { createPlayer } from "./player.js";
import { createTimeline } from "./timeline.js";
import { createControls } from "./controls.js";
import { createExportModal } from "./exportModal.js";
import { createFilePicker } from "./filePicker.js";

function updateHeader() {
  document.getElementById("filename").textContent = state.filename;
  const count = derivedSegments().length;
  document.getElementById("segment-count").textContent = `${count} segment${count === 1 ? "" : "s"}`;
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

async function bootEditor() {
  const data = await api.getState();
  state.filename = data.filename;
  state.duration = data.duration;
  state.videoUrl = data.video_url;
  state.params = data.params;
  state.silences = data.silences;
  loadSegments(data.segments);

  updateHeader();

  const videoEl = document.getElementById("video");
  videoEl.src = state.videoUrl;
  const player = createPlayer(videoEl);

  const timeline = createTimeline({
    viewport: document.getElementById("timeline-viewport"),
    track: document.getElementById("timeline-track"),
    ruler: document.getElementById("ruler"),
    bands: document.getElementById("bands"),
    playhead: document.getElementById("playhead"),
    player,
    splitBtn: document.getElementById("split-btn"),
    deleteSplitBtn: document.getElementById("delete-split-btn"),
    onChange: updateHeader,
  });
  timeline.fit();

  document.getElementById("zoom-in").addEventListener("click", () => timeline.setZoom(timeline.getPxPerSec() * 1.4));
  document.getElementById("zoom-out").addEventListener("click", () => timeline.setZoom(timeline.getPxPerSec() / 1.4));
  document.getElementById("zoom-fit").addEventListener("click", () => timeline.fit());

  const panelEls = [
    document.getElementById("silence-threshold"),
    document.getElementById("min-silence-duration"),
    document.getElementById("min-song-length"),
    document.getElementById("silence-padding"),
    document.getElementById("recompute-btn"),
  ];

  const controls = createControls({
    elements: {
      thresholdInput: document.getElementById("silence-threshold"),
      thresholdValue: document.getElementById("silence-threshold-value"),
      minSilenceInput: document.getElementById("min-silence-duration"),
      minSilenceValue: document.getElementById("min-silence-duration-value"),
      minSongInput: document.getElementById("min-song-length"),
      minSongValue: document.getElementById("min-song-length-value"),
      paddingInput: document.getElementById("silence-padding"),
      paddingValue: document.getElementById("silence-padding-value"),
      recomputeBtn: document.getElementById("recompute-btn"),
      panelEls,
    },
    onSegmentsReplaced: (segments) => {
      loadSegments(segments);
      timeline.render();
      updateHeader();
    },
  });
  controls.initFromState();

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
