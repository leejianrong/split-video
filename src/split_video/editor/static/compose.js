// The compose page: build a project out of several clips on one video track.
// The server (`/api/compose/*`) owns every editing rule; this file only
// draws the project it returns and turns pointer gestures into API calls.

import * as api from "./api.js";

const $ = (id) => document.getElementById(id);
const MIN_CLIP = 0.1;
const MIN_BLOCK_PX = 80;

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
  track: $("track"),
  addClip: $("add-clip-btn"),
  deleteClip: $("delete-clip-btn"),
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

// --- drawing ---------------------------------------------------------------

function videoClips() {
  const track = project.tracks.find((t) => t.kind === "video");
  return track ? track.clips : [];
}

function pxPerSec(clips) {
  const total = clips.reduce((sum, c) => sum + c.duration, 0) || 1;
  const avail = Math.max(300, els.track.clientWidth - 40 - clips.length * 40);
  return Math.max(avail / total, 4);
}

function fmt(seconds) {
  const m = Math.floor(seconds / 60);
  return `${m}:${(seconds - m * 60).toFixed(1).padStart(4, "0")}`;
}

function blockWidth(duration, scale) {
  return Math.max(duration * scale, MIN_BLOCK_PX);
}

function draw() {
  const clips = videoClips();
  els.name.textContent = project.name;
  els.duration.textContent = clips.length ? `${clips.length} clip${clips.length === 1 ? "" : "s"} · ${fmt(project.duration)}` : "";
  els.render.disabled = clips.length === 0;
  els.deleteClip.disabled = !clips.some((c) => c.id === selectedClipId);
  if (!clips.some((c) => c.id === selectedClipId)) selectedClipId = null;

  els.track.innerHTML = "";
  if (!clips.length) {
    const empty = document.createElement("span");
    empty.className = "compose-empty";
    empty.textContent = "No clips yet. Use “Add clip” to pick a video.";
    els.track.appendChild(empty);
    return;
  }
  const scale = pxPerSec(clips);
  clips.forEach((clip, i) => {
    if (i > 0) els.track.appendChild(joinEl(clip));
    els.track.appendChild(clipEl(clip, i, scale));
  });
  syncTransport();
}

function joinEl(clip) {
  const j = document.createElement("button");
  j.className = "join" + (clip.transition === "crossfade" ? " fade" : "") + (transitionClipId === clip.id ? " selected" : "");
  j.dataset.clipId = clip.id;
  j.textContent = clip.transition === "crossfade" ? `${clip.transition_duration.toFixed(1)}s` : "cut";
  j.title = "Set the transition into the next clip";
  j.addEventListener("click", () => openTransitionEditor(clip));
  return j;
}

function clipEl(clip, index, scale) {
  const el = document.createElement("div");
  el.className = "clip" + (clip.id === selectedClipId ? " selected" : "");
  el.dataset.clipId = clip.id;
  el.style.width = `${blockWidth(clip.duration, scale)}px`;
  el.innerHTML = `<div class="clip__edge clip__edge--l"></div><div class="clip__edge clip__edge--r"></div>
    <div class="clip__name"></div><div class="clip__meta"></div>`;
  el.querySelector(".clip__name").textContent = clip.source.split("/").pop();
  el.querySelector(".clip__meta").textContent = `${fmt(clip.in_point)}–${fmt(clip.out_point)} · ${clip.duration.toFixed(1)}s`;
  el.title = clip.source;
  el.addEventListener("pointerdown", (ev) => onPointerDown(ev, el, clip, index, scale));
  return el;
}

// --- gestures: select, trim (edges), reorder (body) -------------------------

function onPointerDown(ev, el, clip, index, scale) {
  if (ev.button !== 0) return;
  selectedClipId = clip.id;
  for (const other of els.track.querySelectorAll(".clip")) other.classList.toggle("selected", other === el);
  els.deleteClip.disabled = false;

  const edge = ev.target.classList.contains("clip__edge--l") ? "l" : ev.target.classList.contains("clip__edge--r") ? "r" : null;
  const startX = ev.clientX;
  let moved = false;
  let trim = null;
  el.setPointerCapture(ev.pointerId);

  const onMove = (e) => {
    const dx = e.clientX - startX;
    if (Math.abs(dx) > 4) moved = true;
    if (!moved) return;
    if (edge) {
      trim = computeTrim(clip, edge, dx / scale);
      el.style.width = `${blockWidth(trim.out_point - trim.in_point, scale)}px`;
      el.querySelector(".clip__meta").textContent = `${fmt(trim.in_point)}–${fmt(trim.out_point)} · ${(trim.out_point - trim.in_point).toFixed(1)}s`;
    } else {
      el.classList.add("dragging");
      el.style.transform = `translateX(${dx}px)`;
    }
  };
  const onUp = async (e) => {
    el.removeEventListener("pointermove", onMove);
    el.removeEventListener("pointerup", onUp);
    el.removeEventListener("pointercancel", onUp);
    if (!moved) return;
    if (edge) {
      await call(`${base()}/clips/${clip.id}`, json("PATCH", trim));
    } else {
      const others = [...els.track.querySelectorAll(".clip")].filter((c) => c !== el);
      const target = others.filter((c) => {
        const r = c.getBoundingClientRect();
        return r.left + r.width / 2 < e.clientX;
      }).length;
      el.style.transform = "";
      if (target !== index) await call(`${base()}/clips/${clip.id}/move`, json("POST", { to_index: target }));
      else draw();
    }
  };
  el.addEventListener("pointermove", onMove);
  el.addEventListener("pointerup", onUp);
  el.addEventListener("pointercancel", onUp);
}

function computeTrim(clip, edge, deltaSeconds) {
  let { in_point, out_point } = clip;
  if (edge === "l") in_point = Math.min(Math.max(0, in_point + deltaSeconds), out_point - MIN_CLIP);
  else out_point = Math.max(Math.min(clip.source_duration ?? Infinity, out_point + deltaSeconds), in_point + MIN_CLIP);
  return { in_point: Math.round(in_point * 100) / 100, out_point: Math.round(out_point * 100) / 100 };
}

// --- transitions -------------------------------------------------------------

function openTransitionEditor(clip) {
  transitionClipId = clip.id;
  els.trLabel.textContent = `Into “${clip.source.split("/").pop()}”`;
  els.trKind.value = clip.transition;
  els.trDuration.value = clip.transition_duration || 1;
  els.trEditor.classList.remove("hidden");
  draw();
}

els.trApply.addEventListener("click", async () => {
  const kind = els.trKind.value;
  await call(`${base()}/clips/${transitionClipId}/transition`, json("PUT", { transition: kind, duration: kind === "cut" ? 0 : Number(els.trDuration.value) }));
});

// --- adding clips -------------------------------------------------------------

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
      await call(`${base()}/clips`, json("POST", { path: entry.path }));
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
