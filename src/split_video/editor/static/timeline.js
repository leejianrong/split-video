// The timeline: a ruler + a row of segment "bands" + draggable split
// markers, rendered as plain positioned DOM nodes (not canvas/SVG) so that
// drag/click/hover hit-testing is free via native pointer events.
//
// Gesture map (kept deliberately non-overlapping):
//   - ruler or band-row click/drag -> seek the video (and deselect)
//   - Split button / "S" key   -> add a split point at the playhead
//   - Delete-split button / Delete/Backspace -> remove the selected split
//   - marker drag (its tab)   -> move that split point
//   - marker hover            -> reveal a delete "x"
//   - marker click             -> select it (arrow keys nudge it +-0.1s,
//                                 +-1s with Shift)
//   - plain wheel over timeline -> zoom, centered on the cursor
//   - Shift+wheel / scrollbar   -> pan (native browser behavior, no JS)
//
// Splits are deliberately a two-step gesture (position the playhead, then
// commit) rather than click-to-add: a single misclick used to be enough to
// litter the timeline with unwanted splits.

import {
  state,
  derivedSegments,
  addSplitPoint,
  canAddSplitPoint,
  removeSplitPointAt,
  moveSplitPointAt,
  formatTime,
} from "./state.js";

const MAX_PX_PER_SEC = 200;
const TICK_INTERVALS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
const MIN_LABEL_SPACING_PX = 70;

export function createTimeline({
  viewport,
  track,
  ruler,
  bands,
  playhead,
  waveformCanvas,
  player,
  splitBtn,
  deleteSplitBtn,
  onChange,
}) {
  let pxPerSec = 1;
  let selectedIndex = null;
  let dragging = null; // { index, pointerId, tabEl }
  let waveformBuckets = [];

  function notifyChange() {
    if (onChange) onChange();
  }

  function updateActionButtons() {
    if (splitBtn) splitBtn.disabled = !canAddSplitPoint(player.currentTime);
    if (deleteSplitBtn) deleteSplitBtn.disabled = selectedIndex === null;
  }

  function addAtPlayhead() {
    const idx = addSplitPoint(player.currentTime);
    if (idx === null) return;
    selectedIndex = idx;
    render();
    notifyChange();
  }

  function deleteSelected() {
    if (selectedIndex === null) return;
    removeSplitPointAt(selectedIndex);
    selectedIndex = null;
    render();
    notifyChange();
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
      selectedIndex = index;
      deleteSelected();
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

  // Unlike the ruler/bands (plain DOM nodes laid out across the full,
  // potentially huge, track width and simply revealed by native scrolling),
  // a canvas that wide would blow past browsers' canvas size limits at
  // typical zoom levels. So this canvas instead stays viewport-sized and
  // redraws its content — which slice of `waveformBuckets` maps to which
  // pixel — from `pxPerSec` and the current scroll position, on every
  // render() (zoom/fit/resize) and on scroll.
  //
  // The canvas element is a positioned child of the *scrolling*
  // `#timeline-viewport`, so — like any other absolutely-positioned
  // descendant of a scroll container (only `fixed`/`sticky` are exempt) —
  // it physically scrolls along with the content instead of staying put.
  // It's counter-translated by `scrollLeft` here so it stays visually
  // pinned over the viewport while its bitmap is redrawn for whatever time
  // range that now puts on screen.
  function renderWaveform() {
    if (!waveformCanvas) return;
    const dpr = window.devicePixelRatio || 1;
    const cssWidth = viewport.clientWidth || 0;
    const cssHeight = 78;
    const scrollLeft = viewport.scrollLeft;
    waveformCanvas.style.width = `${cssWidth}px`;
    waveformCanvas.style.height = `${cssHeight}px`;
    waveformCanvas.style.transform = `translateX(${scrollLeft}px)`;
    waveformCanvas.width = Math.max(1, Math.round(cssWidth * dpr));
    waveformCanvas.height = Math.max(1, Math.round(cssHeight * dpr));

    const ctx = waveformCanvas.getContext("2d");
    ctx.clearRect(0, 0, waveformCanvas.width, waveformCanvas.height);
    if (!waveformBuckets.length || state.duration <= 0) return;

    const bucketCount = waveformBuckets.length;
    const mid = waveformCanvas.height / 2;
    const barWidth = Math.max(1, Math.round(dpr));
    ctx.fillStyle = "rgba(230, 232, 236, 0.4)";

    for (let px = 0; px < cssWidth; px++) {
      const t0 = (scrollLeft + px) / pxPerSec;
      const t1 = (scrollLeft + px + 1) / pxPerSec;
      const bi0 = Math.max(0, Math.min(bucketCount - 1, Math.floor((t0 / state.duration) * bucketCount)));
      const bi1 = Math.max(bi0 + 1, Math.min(bucketCount, Math.ceil((t1 / state.duration) * bucketCount)));

      let lo = 1;
      let hi = -1;
      for (let bi = bi0; bi < bi1; bi++) {
        const [mn, mx] = waveformBuckets[bi];
        if (mn < lo) lo = mn;
        if (mx > hi) hi = mx;
      }
      if (hi < lo) continue;

      const x = Math.round(px * dpr);
      const yTop = mid - hi * mid;
      const yBot = mid - lo * mid;
      ctx.fillRect(x, yTop, barWidth, Math.max(1, yBot - yTop));
    }
  }

  function render() {
    const width = Math.max(state.duration * pxPerSec, viewport.clientWidth || 0);
    track.style.width = `${width}px`;
    renderRuler();
    renderBands();
    renderWaveform();
    updateZoomLabel();
    updateActionButtons();
  }

  function setZoom(newPxPerSec, anchorClientX) {
    const minPx = minPxPerSec();
    const clamped = Math.min(Math.max(newPxPerSec, minPx), MAX_PX_PER_SEC);

    if (anchorClientX != null) {
      const anchorTime = timeFromClientX(anchorClientX);
      pxPerSec = clamped;
      render(); // must run first: it's what widens the track enough for the scrollLeft below to land
      const rect = viewport.getBoundingClientRect();
      viewport.scrollLeft = Math.max(0, anchorTime * pxPerSec - (anchorClientX - rect.left));
      renderWaveform(); // the canvas draws from scrollLeft, so it needs a second pass once that's settled
    } else {
      pxPerSec = clamped;
      render();
    }
  }

  function fit() {
    pxPerSec = minPxPerSec();
    render();
    viewport.scrollLeft = 0;
    renderWaveform();
  }

  function maybeAutoScroll(t) {
    const x = t * pxPerSec;
    const left = viewport.scrollLeft;
    const right = left + viewport.clientWidth;
    if (x < left || x > right) {
      viewport.scrollLeft = Math.max(0, x - viewport.clientWidth / 2);
    }
  }

  // --- ruler + bands: click/drag to seek (and deselect any split) ---
  function seekFromEvent(e) {
    player.seek(timeFromClientX(e.clientX));
  }
  function attachScrubbing(el) {
    el.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".marker")) return;
      seekFromEvent(e);
      selectedIndex = null;
      render();
      const onMove = (ev) => seekFromEvent(ev);
      const onUp = () => {
        el.removeEventListener("pointermove", onMove);
        el.removeEventListener("pointerup", onUp);
      };
      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onUp);
    });
  }
  attachScrubbing(ruler);
  attachScrubbing(bands);

  // --- explicit split add/delete ---
  if (splitBtn) splitBtn.addEventListener("click", addAtPlayhead);
  if (deleteSplitBtn) deleteSplitBtn.addEventListener("click", deleteSelected);

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

  // --- keyboard: add/delete/nudge splits ---
  document.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;

    if (e.key === "s" || e.key === "S") {
      e.preventDefault();
      addAtPlayhead();
      return;
    }

    if (selectedIndex === null) return;

    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      deleteSelected();
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
    updateActionButtons();
  });

  viewport.addEventListener("scroll", () => renderWaveform(), { passive: true });

  window.addEventListener("resize", () => {
    // Re-clamp zoom in case "Fit" is currently active and the window resized.
    if (Math.abs(pxPerSec - minPxPerSec()) < 0.5) fit();
    else renderWaveform();
  });

  return {
    render,
    fit,
    setZoom,
    getPxPerSec: () => pxPerSec,
    setWaveform: (buckets) => {
      waveformBuckets = buckets || [];
      renderWaveform();
    },
  };
}
