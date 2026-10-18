// Thin fetch wrappers around the editor backend's API. Every path is
// relative — the mount path under which this page is served is the
// backend's concern, not ours.

export async function requestJSON(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new Error(`${path}: network error (${err.message})`);
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ? JSON.stringify(body.detail) : JSON.stringify(body);
    } catch (_) {
      // response wasn't JSON; fall back to statusText
    }
    throw new Error(`${path} failed (${res.status}): ${detail}`);
  }

  return res.json();
}

// The project id (the video's path relative to the browse root) every
// project-scoped call carries as a `path` query parameter. It lives only in
// this page's memory, so two tabs can edit two files independently.
let currentPath = null;

export function setProjectPath(path) {
  currentPath = path;
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
  return requestJSON("api/v1/open", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
}

export async function getState() {
  return requestJSON(projectUrl("api/v1/state"));
}

export async function saveProject(segments) {
  return requestJSON(projectUrl("api/v1/project"), {
    method: "POST",
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
