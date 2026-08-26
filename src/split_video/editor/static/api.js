// Thin fetch wrappers around the editor backend's API. Every path is
// relative — the mount path under which this page is served is the
// backend's concern, not ours.

async function requestJSON(path, options = {}) {
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

export async function getSession() {
  return requestJSON("api/session");
}

export async function browse(path = "") {
  return requestJSON(`api/browse?path=${encodeURIComponent(path)}`);
}

export async function openFile(path) {
  return requestJSON("api/open", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
}

export async function getState() {
  return requestJSON("api/state");
}

export async function detectSilence(silenceThreshold) {
  return requestJSON("api/detect", {
    method: "POST",
    body: JSON.stringify({ silence_threshold: silenceThreshold }),
  });
}

export async function getWaveform() {
  return requestJSON("api/waveform");
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
    return await requestJSON("api/segments", {
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
  return requestJSON("api/export", {
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
  return requestJSON(`api/export/${encodeURIComponent(jobId)}`);
}

export async function startAnalysis() {
  return requestJSON("api/analyze", { method: "POST" });
}

export async function getAnalysisStatus(jobId) {
  return requestJSON(`api/analyze/${encodeURIComponent(jobId)}`);
}

export async function getClassification() {
  return requestJSON("api/classification");
}

export async function setClassificationThresholds(thresholds) {
  return requestJSON("api/classification/thresholds", {
    method: "POST",
    body: JSON.stringify({ thresholds }),
  });
}
