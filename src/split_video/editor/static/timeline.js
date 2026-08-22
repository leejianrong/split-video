// The timeline: a ruler + a row of segment "bands" + draggable split
// markers, rendered as plain positioned DOM nodes (not canvas/SVG) so that
// drag/click/hover hit-testing is free via native pointer events.
//
// Gesture map (kept deliberately non-overlapping):
//   - ruler click/drag        -> seek the video
//   - band-row click on empty space -> add a split point there (+ seek)
//   - marker drag (its tab)   -> move that split point
//   - marker hover            -> reveal a delete "x"
//   - marker click             -> select it (Delete/Backspace removes it;
//                                 arrow keys nudge it +-0.1s, +-1s with Shift)
//   - plain wheel over timeline -> zoom, centered on the cursor
//   - Shift+wheel / scrollbar   -> pan (native browser behavior, no JS)

import {
  state,
  derivedSegments,
  addSplitPoint,
  removeSplitPointAt,
  moveSplitPointAt,
  formatTime,
} from "./state.js";

const MAX_PX_PER_SEC = 200;
const TICK_INTERVALS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
const MIN_LABEL_SPACING_PX = 70;

export function createTimeline({ viewport, track, ruler, bands, playhead, player, onChange }) {
  let pxPerSec = 1;
  let selectedIndex = null;
  let dragging = null; // { index, pointerId, tabEl }

  function notifyChange() {
    if (onChange) onChange();
  }

  function minPxPerSec() {
    const width = viewport.clientWidth || 1;
    return Math.max(width / Math.max(state.duration, 1), 0.001);
  }

  function timeFromClientX(clientX) {
    const rect = track.getBoundingClientRect();
    const x = clientX - rect.left;
    return Math.min(Math.max(x / pxPerSec, 0), state.duration);
  }

  function pickTickInterval() {
    for (const interval of TICK_INTERVALS) {
      if (interval * pxPerSec >= MIN_LABEL_SPACING_PX) return interval;
    }
    return TICK_INTERVALS[TICK_INTERVALS.length - 1];
  }

  function updateZoomLabel() {
    const label = document.getElementById("zoom-label");
    if (!label) return;
    label.textContent = Math.abs(pxPerSec - minPxPerSec()) < 0.01 ? "Fit" : `${pxPerSec.toFixed(1)} px/s`;
  }

  function renderRuler() {
    ruler.innerHTML = "";
    const interval = pickTickInterval();
    for (let t = 0; t <= state.duration + 0.001; t += interval) {
      const tick = document.createElement("div");
      tick.className = "tick";
      tick.style.left = `${t * pxPerSec}px`;
      tick.textContent = formatTime(t, state.duration);
      ruler.appendChild(tick);
    }
  }

  function makeBand(startT, endT, cls) {
    const div = document.createElement("div");
    div.className = `band ${cls}`;
    div.style.left = `${startT * pxPerSec}px`;
    div.style.width = `${Math.max(0, (endT - startT) * pxPerSec)}px`;
    return div;
  }

  function makeMarker(t, index) {
    const marker = document.createElement("div");
    marker.className = "marker" + (index === selectedIndex ? " selected" : "");
    marker.style.left = `${t * pxPerSec}px`;
    marker.dataset.splitIndex = String(index);

    const tab = document.createElement("div");
    tab.className = "marker-tab";
    marker.appendChild(tab);

    const del = document.createElement("div");
    del.className = "marker-delete";
    del.textContent = "×";
    del.title = "Delete this split";
    del.addEventListener("pointerdown", (e) => e.stopPropagation());
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      removeSplitPointAt(index);
      selectedIndex = null;
      render();
      notifyChange();
    });
    marker.appendChild(del);

    marker.addEventListener("click", (e) => {
      e.stopPropagation();
      selectedIndex = index;
      render();
    });

    tab.addEventListener("pointerdown", (e) => startDrag(e, index, tab));

    return marker;
  }

  function startDrag(e, index, tabEl) {
    e.stopPropagation();
    e.preventDefault();
    const pointerId = e.pointerId;
    tabEl.setPointerCapture(pointerId);
    dragging = { index, pointerId, tabEl };
    selectedIndex = index;

    const onMove = (ev) => {
      if (!dragging || ev.pointerId !== pointerId) return;
      const t = timeFromClientX(ev.clientX);
      moveSplitPointAt(dragging.index, t);
      render();
    };
    const onUp = (ev) => {
      if (ev.pointerId !== pointerId) return;
      try {
        tabEl.releasePointerCapture(pointerId);
      } catch (_) {
        // already released
      }
      tabEl.removeEventListener("pointermove", onMove);
      tabEl.removeEventListener("pointerup", onUp);
      tabEl.removeEventListener("pointercancel", onUp);
      dragging = null;
      notifyChange();
    };
    tabEl.addEventListener("pointermove", onMove);
    tabEl.addEventListener("pointerup", onUp);
    tabEl.addEventListener("pointercancel", onUp);
  }

  function renderBands() {
    bands.innerHTML = "";

    if (state.firstStart > 0.001) {
      bands.appendChild(makeBand(0, state.firstStart, "excluded"));
    }

    derivedSegments().forEach((seg, i) => {
      bands.appendChild(makeBand(seg.start, seg.end, i % 2 === 0 ? "segment-even" : "segment-odd"));
    });

    if (state.lastEnd < state.duration - 0.001) {
      bands.appendChild(makeBand(state.lastEnd, state.duration, "excluded"));
    }

    state.splitPoints.forEach((t, i) => {
      bands.appendChild(makeMarker(t, i));
    });
  }

  function render() {
    const width = Math.max(state.duration * pxPerSec, viewport.clientWidth || 0);
    track.style.width = `${width}px`;
    renderRuler();
    renderBands();
    updateZoomLabel();
  }

  function setZoom(newPxPerSec, anchorClientX) {
    const minPx = minPxPerSec();
    const clamped = Math.min(Math.max(newPxPerSec, minPx), MAX_PX_PER_SEC);

    if (anchorClientX != null) {
      const anchorTime = timeFromClientX(anchorClientX);
      pxPerSec = clamped;
      render();
      const rect = viewport.getBoundingClientRect();
      viewport.scrollLeft = Math.max(0, anchorTime * pxPerSec - (anchorClientX - rect.left));
    } else {
      pxPerSec = clamped;
      render();
    }
  }

  function fit() {
    pxPerSec = minPxPerSec();
    render();
    viewport.scrollLeft = 0;
  }

  function maybeAutoScroll(t) {
    const x = t * pxPerSec;
    const left = viewport.scrollLeft;
    const right = left + viewport.clientWidth;
    if (x < left || x > right) {
      viewport.scrollLeft = Math.max(0, x - viewport.clientWidth / 2);
    }
  }

  // --- ruler: click/drag to seek ---
  function seekFromEvent(e) {
    player.seek(timeFromClientX(e.clientX));
  }
  ruler.addEventListener("pointerdown", (e) => {
    seekFromEvent(e);
    const onMove = (ev) => seekFromEvent(ev);
    const onUp = () => {
      ruler.removeEventListener("pointermove", onMove);
      ruler.removeEventListener("pointerup", onUp);
    };
    ruler.addEventListener("pointermove", onMove);
    ruler.addEventListener("pointerup", onUp);
  });

  // --- bands: click empty space to add a split point ---
  bands.addEventListener("click", (e) => {
    if (e.target.closest(".marker")) return;
    if (e.target.closest(".excluded")) return;
    const t = timeFromClientX(e.clientX);
    const idx = addSplitPoint(t);
    if (idx !== null) {
      selectedIndex = idx;
      player.seek(t);
      render();
      notifyChange();
    }
  });

  // --- wheel: zoom (plain), pan (shift / native scrollbar) ---
  viewport.addEventListener(
    "wheel",
    (e) => {
      if (e.shiftKey) return; // let native horizontal scroll happen
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
      setZoom(pxPerSec * factor, e.clientX);
    },
    { passive: false }
  );

  // --- keyboard: nudge / delete the selected marker ---
  document.addEventListener("keydown", (e) => {
    if (selectedIndex === null) return;
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;

    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      removeSplitPointAt(selectedIndex);
      selectedIndex = null;
      render();
      notifyChange();
    } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      const step = e.shiftKey ? 1 : 0.1;
      const delta = e.key === "ArrowLeft" ? -step : step;
      moveSplitPointAt(selectedIndex, state.splitPoints[selectedIndex] + delta);
      render();
      notifyChange();
    }
  });

  player.onTimeUpdate((t) => {
    playhead.style.transform = `translateX(${t * pxPerSec}px)`;
    maybeAutoScroll(t);
  });

  window.addEventListener("resize", () => {
    // Re-clamp zoom in case "Fit" is currently active and the window resized.
    if (Math.abs(pxPerSec - minPxPerSec()) < 0.5) fit();
  });

  return {
    render,
    fit,
    setZoom,
    getPxPerSec: () => pxPerSec,
  };
}
