"""Per-process caches over per-file ffmpeg analysis, scoped to one open file.

`SilenceCache` keys its raw silencedetect scans by threshold: only
`silence_threshold` requires a real ffmpeg pass, baked into the decode-time
audio filter. `min_silence_duration`, `min_song_length`, and `padding` are
all pure post-processing over whatever raw silence intervals ffmpeg already
reported (see `split_video.segments.compute_segments`). So a single ffmpeg
scan per distinct threshold, using a very small `d=`, catches every
candidate gap regardless of length — everything else is free.

`WaveformCache` has no such parameter to key on — the peak envelope doesn't
depend on anything user-tunable — so it only ever computes once per file.

`ClassificationCache` is populated by an explicit background job (see
`editor/jobs.py`'s `start_audio_analysis`) rather than lazily on first
request — inference over a long recording takes real time, so it's opt-in
via the editor's "Analyze audio" action. Its `rethreshold` re-derives
regions from the cached per-frame bucket scores, so retuning a threshold
doesn't require rerunning ffmpeg or the model.
"""

from __future__ import annotations

import threading
from pathlib import Path

from split_video.editor import classify
from split_video.editor.classify import ClassificationRegion
from split_video.ffmpeg import detect_silence, extract_pcm_audio
from split_video.silence import SilenceInterval
from split_video.waveform import WaveformPeaks, compute_peaks

RAW_SCAN_MIN_SILENCE_DURATION = 0.05


class SilenceCache:
    """Scoped to one `split-video edit` server process for one source file."""

    def __init__(self, source: Path) -> None:
        self._source = source
        self._raw: dict[float, list[SilenceInterval]] = {}
        self._lock = threading.Lock()

    def get_raw_silences(self, threshold: float) -> list[SilenceInterval]:
        with self._lock:
            if threshold in self._raw:
                return self._raw[threshold]
            intervals = detect_silence(self._source, f"{threshold}dB", RAW_SCAN_MIN_SILENCE_DURATION)
            self._raw[threshold] = intervals
            return intervals


class WaveformCache:
    """Scoped to one `split-video edit` server process for one source file."""

    def __init__(self, source: Path) -> None:
        self._source = source
        self._peaks: WaveformPeaks | None = None
        self._lock = threading.Lock()

    def get_peaks(self) -> WaveformPeaks:
        with self._lock:
            if self._peaks is None:
                self._peaks = compute_peaks(extract_pcm_audio(self._source))
            return self._peaks


class ClassificationCache:
    """Scoped to one `split-video edit` server process for one source file."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame_bucket_scores: list[dict[str, float]] | None = None
        self._total_duration = 0.0
        self._thresholds: dict[str, float] = dict(classify.DEFAULT_THRESHOLDS)
        self._regions: list[ClassificationRegion] = []

    @property
    def is_analyzed(self) -> bool:
        with self._lock:
            return self._frame_bucket_scores is not None

    def get_thresholds(self) -> dict[str, float]:
        with self._lock:
            return dict(self._thresholds)

    def get_regions(self) -> list[ClassificationRegion]:
        with self._lock:
            return list(self._regions)

    def store_result(
        self,
        frame_bucket_scores: list[dict[str, float]],
        total_duration: float,
        regions: list[ClassificationRegion],
    ) -> None:
        with self._lock:
            self._frame_bucket_scores = frame_bucket_scores
            self._total_duration = total_duration
            self._regions = regions

    def rethreshold(self, thresholds: dict[str, float]) -> list[ClassificationRegion]:
        with self._lock:
            if self._frame_bucket_scores is None:
                raise RuntimeError("no analysis result to rethreshold yet")
            self._thresholds = dict(thresholds)
            self._regions = classify.rethreshold(self._frame_bucket_scores, self._thresholds, self._total_duration)
            return list(self._regions)
