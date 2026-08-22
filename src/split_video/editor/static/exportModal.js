// Export confirmation + progress. The preview list's filenames are
// display-only (mirroring the backend's `{index:02d} - {basename}{ext}`
// naming convention) — the backend remains the authority on actual output
// filenames.

import { state, derivedSegments, formatTime } from "./state.js";
import * as api from "./api.js";

const POLL_INTERVAL_MS = 750;

export function createExportModal({ modalEl, overlayEl, openBtn }) {
  function previewFilename(index, total) {
    const width = Math.max(2, String(total).length);
    const ext = state.filename.includes(".") ? state.filename.slice(state.filename.lastIndexOf(".")) : "";
    const base = ext ? state.filename.slice(0, -ext.length) : state.filename;
    return `${String(index).padStart(width, "0")} - ${base}${ext}`;
  }

  function renderConfirm(segments) {
    modalEl.innerHTML = "";
    const box = document.createElement("div");
    box.className = "modal-box";

    const title = document.createElement("h2");
    title.textContent = `Export ${segments.length} segment${segments.length === 1 ? "" : "s"}`;
    box.appendChild(title);

    const preciseLabel = document.createElement("label");
    const preciseCheckbox = document.createElement("input");
    preciseCheckbox.type = "checkbox";
    preciseLabel.appendChild(preciseCheckbox);
    preciseLabel.append(" Frame-accurate cuts (slower re-encode instead of fast stream copy)");
    box.appendChild(preciseLabel);

    const list = document.createElement("ul");
    list.className = "export-preview-list";
    segments.forEach((seg, i) => {
      const li = document.createElement("li");
      const name = previewFilename(i + 1, segments.length);
      li.textContent = `${name} — ${formatTime(seg.start, state.duration)} to ${formatTime(seg.end, state.duration)} (${formatTime(seg.duration, state.duration)})`;
      list.appendChild(li);
    });
    box.appendChild(list);

    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "btn";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", close);
    const confirmBtn = document.createElement("button");
    confirmBtn.className = "btn btn-primary";
    confirmBtn.textContent = "Export";
    confirmBtn.addEventListener("click", () => runExport(segments, preciseCheckbox.checked, box));
    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    box.appendChild(actions);

    modalEl.appendChild(box);
  }

  function open() {
    renderConfirm(derivedSegments());
    modalEl.classList.remove("hidden");
  }

  function close() {
    modalEl.classList.add("hidden");
    modalEl.innerHTML = "";
  }

  async function runExport(segments, precise, box) {
    overlayEl.classList.remove("hidden");
    box.innerHTML = "<p>Starting export…</p>";
    let jobId;
    try {
      const res = await api.startExport({
        segments: segments.map((s) => ({ start: s.start, end: s.end })),
        precise,
      });
      jobId = res.job_id;
    } catch (err) {
      box.innerHTML = `<p class="error">Export failed: ${err.message}</p>`;
      overlayEl.classList.add("hidden");
      return;
    }
    await poll(jobId, box);
  }

  async function poll(jobId, box) {
    for (;;) {
      let status;
      try {
        status = await api.getExportStatus(jobId);
      } catch (err) {
        box.innerHTML = `<p class="error">Export failed: ${err.message}</p>`;
        overlayEl.classList.add("hidden");
        return;
      }

      if (status.status === "running") {
        const current = status.current_file ? `: ${status.current_file}` : "";
        box.innerHTML = `
          <p>Extracting ${status.completed} / ${status.total}${current}</p>
          <progress max="${status.total}" value="${status.completed}"></progress>
        `;
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        continue;
      }

      if (status.status === "done") {
        const fileCount = status.files ? status.files.length : status.total;
        box.innerHTML = `
          <p>Done — wrote ${fileCount} file(s) to <code>${status.output_dir}</code></p>
          <div class="modal-actions">
            <button class="btn btn-primary" id="export-close-btn">Close</button>
          </div>
        `;
        box.querySelector("#export-close-btn").addEventListener("click", () => {
          overlayEl.classList.add("hidden");
          close();
        });
        return;
      }

      // status.status === "error"
      box.innerHTML = `<p class="error">Export failed: ${status.error || "unknown error"}</p>`;
      overlayEl.classList.add("hidden");
      return;
    }
  }

  openBtn.addEventListener("click", open);

  return { open, close };
}
