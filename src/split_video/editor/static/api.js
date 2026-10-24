// Thin fetch wrappers around the editor backend's API. Every path is
// relative — the mount path under which this page is served is the
// backend's concern, not ours.

// Shown (non-blocking, in the page's status line) when a save was refused because the project was
// changed elsewhere (the CLI, an agent, another tab) since this page last loaded it.
export const CONFLICT_MESSAGE =
  "Project changed elsewhere; reloaded the latest version, please redo your last change.";

// The `If-Match` header that makes a write conditional on the revision the page last saw.
export function ifMatch(revision) {
  return revision === null || revision === undefined ? {} : { "If-Match": `"${revision}"` };
}

export async function requestJSON(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new Error(`${path}: network error (${err.message})`);
  }

  if (!res.ok) {
    let detail = res.statusText;
    let body = null;
    try {
      body = await res.json();
      detail = body.detail ? JSON.stringify(body.detail) : JSON.stringify(body);
    } catch (_) {
      // response wasn't JSON; fall back to statusText
    }
    const err = new Error(`${path} failed (${res.status}): ${detail}`);
    err.status = res.status;
    err.body = body;
    // A stale write (see editor/revisions.py), as opposed to any other 409 such as "output exists".
    err.isRevisionConflict = res.status === 409 && !!body && body.error === "revision_conflict";
    throw err;
  }

  return res.json();
}

// The project id (the video's path relative to the browse root) every
// project-scoped call carries as a `path` query parameter. It lives only in
// this page's memory, so two tabs can edit two files independently.
let currentPath = null;
// The revision of the open project as of the last response that carried one. Writes send it as
// `If-Match`, so a save made after someone else changed the file is refused instead of overwriting it.
let currentRevision = null;

export function setProjectPath(path) {
  currentPath = path;
  currentRevision = null;
}

export function getRevision() {
  return currentRevision;
}

function trackRevision(state) {
  if (state && Number.isInteger(state.revision)) currentRevision = state.revision;
  return state;
}

function projectUrl(base) {
  if (currentPath === null) throw new Error("no project selected");
  return `${base}?path=${encodeURIComponent(currentPath)}`;
}

export async function getSession() {
  return requestJSON("api/v1/session");
}

export async function browse(path = "") {
  return requestJSON(`api/v1/browse?path=${encodeURIComponent(path)}`);
}

export async function openFile(path) {
  setProjectPath(path);
  return trackRevision(
    await requestJSON("api/v1/open", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  );
}

export async function getState() {
  return trackRevision(await requestJSON(projectUrl("api/v1/state")));
}

// Saves run one at a time: each reads the revision the previous one returned, so two quick edits
// don't send the same `If-Match` and have the second refused as a conflict with the first.
let saveChain = Promise.resolve();

export function saveProject(segments) {
  const run = saveChain.then(() => doSaveProject(segments));
  saveChain = run.catch(() => {});
  return run;
}

async function doSaveProject(segments) {
  const saved = await requestJSON(projectUrl("api/v1/project"), {
    method: "POST",
    headers: ifMatch(currentRevision),
    body: JSON.stringify({
      segments: segments.map((s) => ({
        start: s.start,
        end: s.end,
        label: s.label || "",
        color: s.color || null,
        included: s.included !== false,
        export_name: s.exportName || null,
      })),
    }),
  });
  return trackRevision(saved);
}

export async function detectSilence(silenceThreshold) {
  return requestJSON(projectUrl("api/v1/detect"), {
    method: "POST",
    body: JSON.stringify({ silence_threshold: silenceThreshold }),
  });
}

export async function getWaveform() {
  return requestJSON(projectUrl("api/v1/waveform"));
}

// Only one segments recompute should ever be in flight: a new call aborts
// whatever the previous call was waiting on, so fast slider dragging can't
// produce an out-of-order response clobbering a newer one.
let segmentsAbortController = null;

export async function computeSegments({ silences, duration, minSilenceDuration, minSongLength, padding }) {
  if (segmentsAbortController) segmentsAbortController.abort();
  segmentsAbortController = new AbortController();
  const { signal } = segmentsAbortController;

  try {
    return await requestJSON("api/v1/segments", {
      method: "POST",
      body: JSON.stringify({
        silences,
        duration,
        min_silence_duration: minSilenceDuration,
        min_song_length: minSongLength,
        padding,
      }),
      signal,
    });
  } catch (err) {
    if (err.name === "AbortError") return null;
    throw err;
  }
}

export async function startExport({ segments, precise, outputFormat, overwrite, manifest }) {
  return requestJSON(projectUrl("api/v1/export"), {
    method: "POST",
    body: JSON.stringify({
      segments,
      precise: !!precise,
      output_format: outputFormat ?? null,
      overwrite: overwrite ?? false,
      manifest: manifest ?? true,
    }),
  });
}

export async function getExportStatus(jobId) {
  return requestJSON(`api/v1/export/${encodeURIComponent(jobId)}`);
}

export async function startAnalysis() {
  return requestJSON(projectUrl("api/v1/analyze"), { method: "POST" });
}

export async function getAnalysisStatus(jobId) {
  return requestJSON(`api/v1/analyze/${encodeURIComponent(jobId)}`);
}

export async function getClassification() {
  return requestJSON(projectUrl("api/v1/classification"));
}

export async function setClassificationThresholds(thresholds) {
  return requestJSON(projectUrl("api/v1/classification/thresholds"), {
    method: "POST",
    body: JSON.stringify({ thresholds }),
  });
}
