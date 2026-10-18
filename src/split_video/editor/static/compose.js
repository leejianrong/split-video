// The compose page: arrange clips on a multi-track timeline.
// The server (`/api/compose/*`) owns every editing rule; this file only
// draws the project it returns and turns pointer gestures into API calls.

import * as api from "./api.js";

const $ = (id) => document.getElementById(id);
const MIN_CLIP = 0.1;
const SNAP = 0.1; // seconds a dragged clip snaps to
const HEAD_ROW_PX = 60;

const els = {
  name: $("project-name"),
  duration: $("project-duration"),
  status: $("status"),
  render: $("render-btn"),
  picker: $("project-picker"),
  list: $("project-list"),
  newForm: $("new-project-form"),
  newName: $("new-project-name"),
  main: $("compose-main"),
  timeline: $("timeline"),
  addClip: $("add-clip-btn"),
  addVideoTrack: $("add-video-track-btn"),
  addAudioTrack: $("add-audio-track-btn"),
  deleteClip: $("delete-clip-btn"),
  zoomIn: $("zoom-in-btn"),
  zoomOut: $("zoom-out-btn"),
  zoomFit: $("zoom-fit-btn"),
  clipPicker: $("clip-picker"),
  pickerUp: $("picker-up"),
  pickerPath: $("picker-path"),
  pickerList: $("picker-list"),
  trEditor: $("transition-editor"),
  trLabel: $("transition-label"),
  trKind: $("transition-kind"),
  trDuration: $("transition-duration"),
  trApply: $("transition-apply"),
  renderStatus: $("render-status"),
  play: $("play-btn"),
  scrub: $("scrub"),
  time: $("time-label"),
  video: $("video"),
};

let projectName = null;
let project = null;
let selectedClipId = null;
let transitionClipId = null;
let activeTrackId = null; // where "Add clip" puts things
let zoom = 1; // multiplier on the fit-to-width scale

const base = () => `api/compose/projects/${encodeURIComponent(projectName)}`;
const json = (method, body) => ({ method, body: body === undefined ? undefined : JSON.stringify(body) });

function setStatus(text, isError = false) {
  els.status.textContent = text;
  els.status.classList.toggle("error", isError);
}

async function call(path, options) {
  try {
    project = await api.requestJSON(path, options);
    setStatus("");
    draw();
  } catch (err) {
    setStatus(err.message, true);
    await reload();
  }
}

async function reload() {
  project = await api.requestJSON(base());
  draw();
}

// --- model helpers ---------------------------------------------------------

// The main track is the first video track when it keeps clips back to back.
function mainTrack() {
  const first = project.tracks.find((t) => t.kind === "video");
  return first && first.magnetic ? first : null;
}

// What the preview player plays: the main track's clips.
function videoClips() {
  const main = mainTrack();
  return main ? main.clips : [];
}

function allClips() {
  return project.tracks.flatMap((t) => t.clips);
}

function trackLabel(track) {
  if (track === mainTrack()) return ["Main", "video"];
  const same = project.tracks.filter((t) => t.kind === track.kind && t !== mainTrack());
  return [`${track.kind === "video" ? "Video" : "Audio"} ${same.indexOf(track) + (mainTrack() && track.kind === "video" ? 2 : 1)}`, track.kind];
}

// Lanes top to bottom: upper video layers (topmost first), the main track, then audio.
function laneOrder() {
  const videos = project.tracks.filter((t) => t.kind === "video");
  const audios = project.tracks.filter((t) => t.kind === "audio");
  return [...videos.slice().reverse(), ...audios];
}

function fmt(seconds) {
  const m = Math.floor(seconds / 60);
  return `${m}:${(seconds - m * 60).toFixed(1).padStart(4, "0")}`;
}

// --- layout ------------------------------------------------------------------

let pps = 20; // pixels per second at the current zoom

function timelineSeconds() {
  return Math.max(project.duration + 2, 10);
}

function computeScale() {
  const scroll = els.timeline.querySelector(".tl__scroll");
  const avail = Math.max(300, (scroll ? scroll.clientWidth : els.timeline.clientWidth - 190) - 24);
  pps = Math.max((avail / timelineSeconds()) * zoom, 2);
}

function tickStep() {
  for (const step of [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]) if (step * pps >= 70) return step;
  return 900;
}

// --- drawing ---------------------------------------------------------------------

function draw() {
  const clips = allClips();
  els.name.textContent = project.name;
  els.duration.textContent = clips.length ? `${clips.length} clip${clips.length === 1 ? "" : "s"} · ${fmt(project.duration)}` : "";
  els.render.disabled = !project.tracks.some((t) => t.kind === "video" && t.clips.length);
  if (!clips.some((c) => c.id === selectedClipId)) selectedClipId = null;
  els.deleteClip.disabled = selectedClipId === null;
  if (!project.tracks.some((t) => t.id === activeTrackId)) activeTrackId = (mainTrack() || project.tracks[0]).id;

  els.timeline.innerHTML = '<div class="tl__heads"></div><div class="tl__scroll"><div class="tl__canvas"></div></div>';
  computeScale();
  const heads = els.timeline.querySelector(".tl__heads");
  const canvas = els.timeline.querySelector(".tl__canvas");
  canvas.style.width = `${timelineSeconds() * pps}px`;

  heads.appendChild(Object.assign(document.createElement("div"), { className: "tl__headspacer" }));
  canvas.appendChild(rulerEl());
  for (const track of laneOrder()) {
    heads.appendChild(headEl(track));
    canvas.appendChild(laneEl(track));
  }
  const playheadEl = Object.assign(document.createElement("div"), { className: "tl__playhead", id: "playhead-line" });
  canvas.appendChild(playheadEl);
  positionPlayhead();
  syncTransport();
}

function rulerEl() {
  const ruler = Object.assign(document.createElement("div"), { className: "tl__ruler" });
  const step = tickStep();
  for (let t = 0; t <= timelineSeconds(); t += step) {
    const tick = Object.assign(document.createElement("div"), { className: "tl__tick", textContent: fmt(t) });
    tick.style.left = `${t * pps}px`;
    ruler.appendChild(tick);
  }
  ruler.addEventListener("pointerdown", (ev) => {
    const rect = ruler.getBoundingClientRect();
    scrubTo((ev.clientX - rect.left) / pps);
  });
  return ruler;
}

function headEl(track) {
  const head = Object.assign(document.createElement("div"), { className: "lanehead" + (track.id === activeTrackId ? " active" : "") });
  const [label, kind] = trackLabel(track);
  head.innerHTML = '<span class="lanehead__name"></span>';
  head.querySelector(".lanehead__name").innerHTML = `${label} <span class="lanehead__kind"></span>`;
  head.querySelector(".lanehead__kind").textContent = kind;
  head.addEventListener("click", () => {
    activeTrackId = track.id;
    draw();
  });
  const button = (text, title, disabled, handler) => {
    const b = Object.assign(document.createElement("button"), { textContent: text, title, disabled });
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      handler();
    });
    head.appendChild(b);
  };
  const order = laneOrder();
  const sameKindNeighbors = project.tracks.filter((t) => t.kind === track.kind);
  const at = sameKindNeighbors.indexOf(track);
  if (track.kind === "video") {
    const isMain = track === mainTrack();
    button("▲", "Move up the stack", isMain || at === sameKindNeighbors.length - 1, () =>
      call(`${base()}/tracks/${track.id}/move`, json("POST", { to_index: project.tracks.indexOf(sameKindNeighbors[at + 1]) })));
    button("▼", "Move down the stack", isMain || at <= (mainTrack() ? 1 : 0), () =>
      call(`${base()}/tracks/${track.id}/move`, json("POST", { to_index: project.tracks.indexOf(sameKindNeighbors[at - 1]) })));
  }
  const onlyVideo = track.kind === "video" && sameKindNeighbors.length === 1;
  button("×", "Remove this track", onlyVideo || order.length === 1, () => {
    if (!track.clips.length || confirm("Remove this track and its clips from the project?")) call(`${base()}/tracks/${track.id}`, json("DELETE"));
  });
  return head;
}

function laneEl(track) {
  const lane = Object.assign(document.createElement("div"), { className: "lane" + (track.id === activeTrackId ? " active" : "") });
  lane.dataset.trackId = track.id;
  lane.dataset.kind = track.kind;
  lane.dataset.magnetic = track.magnetic ? "1" : "";
  track.clips.forEach((clip, index) => {
    if (track.magnetic && index > 0) lane.appendChild(joinEl(clip));
    lane.appendChild(clipEl(track, clip, index));
  });
  return lane;
}

function joinEl(clip) {
  const j = document.createElement("button");
  j.className = "join on-lane" + (clip.transition === "crossfade" ? " fade" : "") + (transitionClipId === clip.id ? " selected" : "");
  j.style.left = `${clip.start * pps - 17}px`;
  j.textContent = clip.transition === "crossfade" ? `${clip.transition_duration.toFixed(1)}s` : "cut";
  j.title = "Set the transition into this clip";
  j.addEventListener("click", () => openTransitionEditor(clip));
  return j;
}

function clipLabel(clip) {
  return clip.kind === "text" ? `“${clip.text}”` : clip.source.split("/").pop();
}

function clipEl(track, clip, index) {
  const el = document.createElement("div");
  el.className = "clip" + (track.kind === "audio" ? " audio" : "") + (clip.kind === "text" ? " text" : "") + (clip.id === selectedClipId ? " selected" : "");
  el.dataset.clipId = clip.id;
  el.style.left = `${clip.start * pps}px`;
  el.style.width = `${Math.max(clip.duration * pps, 14)}px`;
  el.innerHTML = '<div class="clip__edge clip__edge--l"></div><div class="clip__edge clip__edge--r"></div><div class="clip__name"></div><div class="clip__meta"></div>';
  el.querySelector(".clip__name").textContent = clipLabel(clip);
  el.querySelector(".clip__meta").textContent = `${clip.duration.toFixed(1)}s`;
  el.title = clip.kind === "text" ? clip.text : clip.source;
  el.addEventListener("pointerdown", (ev) => onPointerDown(ev, el, track, clip, index));
  return el;
}

// --- gestures: select, trim (edges), reorder or move (body) --------------------------

function laneAt(clientX, clientY) {
  for (const lane of els.timeline.querySelectorAll(".lane")) {
    const r = lane.getBoundingClientRect();
    if (clientY >= r.top && clientY < r.bottom) return lane;
  }
  return null;
}

// Would a clip occupying [start, start+duration) overlap another on `track`?
function overlaps(track, clipId, start, duration) {
  return track.clips.some((c) => c.id !== clipId && start < c.end - 1e-6 && start + duration > c.start + 1e-6);
}

function trimmed(clip, edge, deltaSeconds, track) {
  let { in_point, out_point, start } = clip;
  const limit = clip.kind === "text" ? Infinity : clip.source_duration ?? Infinity;
  if (edge === "l") {
    const d = Math.min(Math.max(deltaSeconds, -in_point, clip.kind === "text" ? -start : -Infinity), out_point - MIN_CLIP - in_point);
    in_point += d;
    if (!track.magnetic) start += d;
  } else {
    out_point = Math.max(Math.min(limit, out_point + deltaSeconds), in_point + MIN_CLIP);
  }
  const round = (v) => Math.round(v * 100) / 100;
  return { in_point: round(in_point), out_point: round(out_point), start: round(start) };
}

function onPointerDown(ev, el, track, clip, index) {
  if (ev.button !== 0) return;
  selectedClipId = clip.id;
  for (const other of els.timeline.querySelectorAll(".clip")) other.classList.toggle("selected", other === el);
  els.deleteClip.disabled = false;
  activeTrackId = track.id;

  const edge = ev.target.classList.contains("clip__edge--l") ? "l" : ev.target.classList.contains("clip__edge--r") ? "r" : null;
  const startX = ev.clientX;
  const startY = ev.clientY;
  let moved = false;
  let result = null; // what to do on release
  el.setPointerCapture(ev.pointerId);

  const onMove = (e) => {
    const dx = e.clientX - startX;
    if (Math.hypot(dx, e.clientY - startY) > 4) moved = true;
    if (!moved) return;
    const dt = dx / pps;
    if (edge) {
      const t = trimmed(clip, edge, dt, track);
      const duration = t.out_point - t.in_point;
      el.style.left = `${t.start * pps}px`;
      el.style.width = `${Math.max(duration * pps, 14)}px`;
      el.querySelector(".clip__meta").textContent = `${duration.toFixed(1)}s`;
      const bad = !track.magnetic && overlaps(track, clip.id, t.start, duration);
      el.classList.toggle("invalid", bad);
      result = bad ? null : { type: "trim", t };
    } else if (track.magnetic) {
      el.classList.add("dragging");
      el.style.transform = `translateX(${dx}px)`;
      const others = [...els.timeline.querySelectorAll(`.lane[data-track-id="${track.id}"] .clip`)].filter((c) => c !== el);
      const target = others.filter((c) => {
        const r = c.getBoundingClientRect();
        return r.left + r.width / 2 < e.clientX;
      }).length;
      result = target !== index ? { type: "reorder", target } : null;
    } else {
      el.classList.add("dragging");
      const lane = laneAt(e.clientX, e.clientY);
      const dest = lane && lane.dataset.kind === track.kind && !lane.dataset.magnetic ? project.tracks.find((t) => t.id === lane.dataset.trackId) : track;
      const start = Math.max(0, Math.round((clip.start + dt) / SNAP) * SNAP);
      const dy = lane ? lane.getBoundingClientRect().top - el.parentElement.getBoundingClientRect().top : 0;
      el.style.transform = `translate(${(start - clip.start) * pps}px, ${dest === track ? 0 : dy}px)`;
      const bad = overlaps(dest, clip.id, start, clip.duration);
      el.classList.toggle("invalid", bad);
      result = bad ? null : { type: "place", start: Math.round(start * 100) / 100, trackId: dest.id };
    }
  };
  const onUp = async () => {
    el.removeEventListener("pointermove", onMove);
    el.removeEventListener("pointerup", onUp);
    el.removeEventListener("pointercancel", onUp);
    if (!moved) return;
    if (!result) {
      setStatus(edge ? "That would overlap another clip." : "Can't drop the clip there.", true);
      return draw();
    }
    if (result.type === "trim") await call(`${base()}/clips/${clip.id}`, json("PATCH", { in_point: result.t.in_point, out_point: result.t.out_point }));
    else if (result.type === "reorder") await call(`${base()}/clips/${clip.id}/move`, json("POST", { to_index: result.target }));
    else await call(`${base()}/clips/${clip.id}/place`, json("POST", { start: result.start, track_id: result.trackId }));
  };
  el.addEventListener("pointermove", onMove);
  el.addEventListener("pointerup", onUp);
  el.addEventListener("pointercancel", onUp);
}

// --- zoom -------------------------------------------------------------------------------

const rezoom = (factor) => {
  zoom = factor === null ? 1 : Math.min(Math.max(zoom * factor, 0.2), 40);
  draw();
};
els.zoomIn.addEventListener("click", () => rezoom(1.5));
els.zoomOut.addEventListener("click", () => rezoom(1 / 1.5));
els.zoomFit.addEventListener("click", () => rezoom(null));

// --- transitions -------------------------------------------------------------------------

function openTransitionEditor(clip) {
  transitionClipId = clip.id;
  els.trLabel.textContent = `Into “${clipLabel(clip)}”`;
  els.trKind.value = clip.transition;
  els.trDuration.value = clip.transition_duration || 1;
  els.trEditor.classList.remove("hidden");
  draw();
}

els.trApply.addEventListener("click", async () => {
  const kind = els.trKind.value;
  await call(`${base()}/clips/${transitionClipId}/transition`, json("PUT", { transition: kind, duration: kind === "cut" ? 0 : Number(els.trDuration.value) }));
});

// --- adding clips and tracks ---------------------------------------------------------------

let pickerParent = null;

async function loadPicker(path) {
  const listing = await api.browse(path);
  pickerParent = listing.parent;
  els.pickerPath.textContent = "/" + listing.cwd;
  els.pickerUp.disabled = pickerParent === null;
  els.pickerList.innerHTML = "";
  for (const entry of listing.entries) {
    const li = document.createElement("li");
    li.className = entry.is_dir ? "dir" : "file";
    li.innerHTML = '<span class="name"></span>';
    li.querySelector(".name").textContent = entry.name;
    li.addEventListener("click", async () => {
      if (entry.is_dir) return loadPicker(entry.path);
      const track = project.tracks.find((t) => t.id === activeTrackId);
      const body = { path: entry.path, track_id: track.id };
      if (!track.magnetic) body.start = Math.round(playhead * 100) / 100;
      await call(`${base()}/clips`, json("POST", body));
    });
    els.pickerList.appendChild(li);
  }
}

els.addClip.addEventListener("click", async () => {
  const opening = els.clipPicker.classList.contains("hidden");
  els.clipPicker.classList.toggle("hidden", !opening);
  if (opening) await loadPicker("");
});
els.pickerUp.addEventListener("click", () => pickerParent !== null && loadPicker(pickerParent));
els.addVideoTrack.addEventListener("click", () => call(`${base()}/tracks`, json("POST", { kind: "video" })));
els.addAudioTrack.addEventListener("click", () => call(`${base()}/tracks`, json("POST", { kind: "audio" })));

els.deleteClip.addEventListener("click", async () => {
  if (selectedClipId) await call(`${base()}/clips/${selectedClipId}`, json("DELETE"));
});

document.addEventListener("keydown", (ev) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(ev.target.tagName)) return;
  if ((ev.key === "Delete" || ev.key === "Backspace") && selectedClipId) {
    ev.preventDefault();
    call(`${base()}/clips/${selectedClipId}`, json("DELETE"));
  }
});

// --- rendering ------------------------------------------------------------------

els.render.addEventListener("click", async () => {
  els.renderStatus.classList.remove("hidden");
  els.renderStatus.textContent = "Starting render…";
  els.render.disabled = true;
  try {
    let started;
    try {
      started = await api.requestJSON(`${base()}/render`, json("POST", {}));
    } catch (err) {
      if (!String(err.message).includes("(409)")) throw err;
      if (!confirm("The render file already exists. Overwrite it?")) throw new Error("Render cancelled.");
      started = await api.requestJSON(`${base()}/render`, json("POST", { overwrite: true }));
    }
    for (;;) {
      const s = await api.requestJSON(`api/compose/render/${started.job_id}`);
      if (s.status === "running") {
        els.renderStatus.textContent = `Rendering… ${Math.round(s.progress * 100)}%`;
        await new Promise((r) => setTimeout(r, 500));
      } else if (s.status === "done") {
        els.renderStatus.textContent = `Done: ${s.output} (next to your source videos)`;
        break;
      } else {
        throw new Error(s.error || "render failed");
      }
    }
  } catch (err) {
    els.renderStatus.textContent = `Render failed: ${err.message}`;
  } finally {
    els.render.disabled = videoClips().length === 0;
  }
});

// --- preview: play the clips back to back -------------------------------------
//
// The playhead is a time on the project timeline. Each clip owns the stretch
// from its own start to the next clip's start, so a crossfade previews as a
// cut at the point where the rendered video would start blending, and the
// preview's length matches the render's.

let playhead = 0;
let playing = false;
let loadedClipId = null;
let rafId = null;

function clipSpan(clips, i) {
  const clip = clips[i];
  const until = i + 1 < clips.length ? clips[i + 1].start : clip.end;
  return { from: clip.start, until };
}

function clipIndexAt(clips, t) {
  for (let i = 0; i < clips.length; i++) {
    if (t < clipSpan(clips, i).until - 1e-6) return i;
  }
  return clips.length - 1;
}

function showTime() {
  els.time.textContent = `${fmt(playhead)} / ${fmt(project ? project.duration : 0)}`;
  els.scrub.value = playhead;
  positionPlayhead();
}

function positionPlayhead() {
  const line = $("playhead-line");
  if (line) line.style.left = `${playhead * pps}px`;
}

function scrubTo(t) {
  if (videoClips().length) seekTo(t, { thenPlay: playing });
  else {
    playhead = Math.min(Math.max(0, t), project.duration);
    showTime();
  }
}

// Point the player at the clip under `t` and seek to the matching source time.
function seekTo(t, { thenPlay = false } = {}) {
  const clips = videoClips();
  if (!clips.length) return;
  playhead = Math.min(Math.max(0, t), project.duration);
  const i = clipIndexAt(clips, playhead);
  const clip = clips[i];
  const sourceTime = clip.in_point + (playhead - clip.start);
  const ready = () => {
    els.video.currentTime = sourceTime;
    if (thenPlay) els.video.play().catch(() => setPlaying(false));
  };
  if (loadedClipId === clip.id && els.video.readyState >= 1) {
    ready();
  } else {
    loadedClipId = clip.id;
    const url = `media/file?path=${encodeURIComponent(clip.source)}`;
    els.video.addEventListener("loadedmetadata", ready, { once: true });
    els.video.src = url;
  }
  showTime();
}

function tick() {
  const clips = videoClips();
  if (!playing || !clips.length) return;
  const i = clipIndexAt(clips, playhead);
  const clip = clips[i];
  const span = clipSpan(clips, i);
  if (loadedClipId === clip.id && !els.video.seeking && els.video.readyState >= 2) {
    playhead = clip.start + (els.video.currentTime - clip.in_point);
    if (playhead >= span.until - 0.02) {
      if (i + 1 < clips.length) {
        seekTo(span.until, { thenPlay: true });
      } else {
        setPlaying(false);
        playhead = project.duration;
        els.video.pause();
      }
    }
    showTime();
  }
  rafId = requestAnimationFrame(tick);
}

function setPlaying(on) {
  playing = on;
  els.play.textContent = on ? "Pause" : "Play";
  cancelAnimationFrame(rafId);
  if (on) rafId = requestAnimationFrame(tick);
  else els.video.pause();
}

function togglePlay() {
  if (!videoClips().length) return;
  if (playing) return setPlaying(false);
  if (playhead >= project.duration - 0.05) playhead = 0;
  setPlaying(true);
  seekTo(playhead, { thenPlay: true });
}

// Called after every redraw: keep the scrubber's range and playhead valid, and
// show the frame at the playhead if the clip under it changed (an edit).
function syncTransport() {
  const clips = videoClips();
  els.scrub.max = project.duration;
  els.play.disabled = !clips.length;
  els.scrub.disabled = !clips.length;
  playhead = Math.min(playhead, project.duration);
  showTime();
  const clip = clips[clipIndexAt(clips, playhead)];
  if (clip && !playing && (loadedClipId !== clip.id || selectedClipId === null)) seekTo(playhead);
}

els.play.addEventListener("click", togglePlay);
els.scrub.addEventListener("input", () => seekTo(Number(els.scrub.value), { thenPlay: playing }));
document.addEventListener("keydown", (ev) => {
  if (ev.code === "Space" && !["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(ev.target.tagName)) {
    ev.preventDefault();
    togglePlay();
  }
});

// --- boot --------------------------------------------------------------------------

async function showPicker() {
  const { projects } = await api.requestJSON("api/compose/projects");
  els.picker.classList.remove("hidden");
  els.main.classList.add("hidden");
  els.list.innerHTML = "";
  for (const name of projects) {
    const li = document.createElement("li");
    li.className = "file";
    li.innerHTML = '<span class="name"></span>';
    li.querySelector(".name").textContent = name;
    li.addEventListener("click", () => (location.search = `?project=${encodeURIComponent(name)}`));
    els.list.appendChild(li);
  }
}

els.newForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const created = await api.requestJSON("api/compose/projects", json("POST", { name: els.newName.value.trim() }));
    location.search = `?project=${encodeURIComponent(created.name)}`;
  } catch (err) {
    setStatus(err.message, true);
  }
});

window.addEventListener("resize", () => project && draw());

async function boot() {
  projectName = new URLSearchParams(location.search).get("project");
  if (!projectName) return showPicker();
  els.main.classList.remove("hidden");
  try {
    await reload();
  } catch (err) {
    els.main.classList.add("hidden");
    setStatus(err.message, true);
    await showPicker();
  }
}

boot();
