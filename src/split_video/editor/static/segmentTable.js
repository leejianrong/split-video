// The segment table: one row per current segment, doubling as both the
// label/color editor (#19) and the export preview — include/exclude plus a
// custom output name, with a live "N of M" count (#18). Reads/writes
// state.js's per-segment metadata directly; the timeline's bands reflect
// the same color/label/included state, so this and the timeline never
// disagree about what a segment is or whether it'll be exported.

import { state, derivedSegments, updateSegmentMeta, formatTime } from "./state.js";

// Matches the band's own default (--accent-dim in styles.css) so a color
// swatch shown here before the user has picked anything doesn't disagree
// with what the timeline is actually drawing.
const DEFAULT_COLOR = "#2b6cb0";

function defaultExportBasename(index, total, filename) {
  const width = Math.max(2, String(total).length);
  const dot = filename.lastIndexOf(".");
  const base = dot > 0 ? filename.slice(0, dot) : filename;
  return `${String(index).padStart(width, "0")} - ${base}`;
}

export function createSegmentTable({ bodyEl, countEl, onChange }) {
  function render() {
    const segments = derivedSegments();
    bodyEl.innerHTML = "";

    segments.forEach((seg, i) => {
      const tr = document.createElement("tr");
      tr.classList.toggle("excluded-row", !seg.included);

      const checkTd = document.createElement("td");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.className = "seg-check";
      check.checked = seg.included;
      check.title = "Include in export";
      check.addEventListener("change", () => {
        updateSegmentMeta(i, { included: check.checked });
        onChange();
      });
      checkTd.appendChild(check);
      tr.appendChild(checkTd);

      const nameTd = document.createElement("td");
      const wrap = document.createElement("div");
      wrap.className = "seg-name-wrap";

      const colorInput = document.createElement("input");
      colorInput.type = "color";
      colorInput.className = "seg-color";
      colorInput.title = "Segment color";
      colorInput.value = seg.color || DEFAULT_COLOR;
      // "change" (fires once the picker closes), not "input" (fires
      // continuously while dragging inside it) — onChange() below causes a
      // full table re-render, which would otherwise tear down this very
      // input mid-interaction.
      colorInput.addEventListener("change", () => {
        updateSegmentMeta(i, { color: colorInput.value });
        onChange();
      });

      const labelInput = document.createElement("input");
      labelInput.type = "text";
      labelInput.className = "seg-label";
      labelInput.placeholder = `Segment ${i + 1}`;
      labelInput.value = seg.label;
      labelInput.addEventListener("change", () => {
        updateSegmentMeta(i, { label: labelInput.value.trim() });
        onChange();
      });

      wrap.appendChild(colorInput);
      wrap.appendChild(labelInput);
      nameTd.appendChild(wrap);
      tr.appendChild(nameTd);

      const rangeTd = document.createElement("td");
      rangeTd.className = "mono";
      rangeTd.textContent = `${formatTime(seg.start, state.duration)}–${formatTime(seg.end, state.duration)}`;
      tr.appendChild(rangeTd);

      const lengthTd = document.createElement("td");
      lengthTd.className = "mono";
      lengthTd.textContent = formatTime(seg.duration, state.duration);
      tr.appendChild(lengthTd);

      const exportTd = document.createElement("td");
      const exportInput = document.createElement("input");
      exportInput.type = "text";
      exportInput.className = "seg-export-name mono";
      exportInput.placeholder = defaultExportBasename(i + 1, segments.length, state.filename);
      exportInput.value = seg.exportName || "";
      exportInput.addEventListener("change", () => {
        updateSegmentMeta(i, { exportName: exportInput.value.trim() || null });
        onChange();
      });
      exportTd.appendChild(exportInput);
      tr.appendChild(exportTd);

      bodyEl.appendChild(tr);
    });

    updateCount(segments);
  }

  function updateCount(segments) {
    const included = segments.filter((s) => s.included).length;
    countEl.textContent = `Exporting ${included} of ${segments.length} segment${segments.length === 1 ? "" : "s"}`;
  }

  return { render };
}
