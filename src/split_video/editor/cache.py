"""Per-process caches over per-file ffmpeg analysis, scoped to one open file.

`SilenceCache` keys its raw silencedetect scans by threshold: only
`silence_threshold` requires a real ffmpeg pass, baked into the decode-time
audio filter. `min_silence_duration`, `min_song_length`, and `padding` are
all pure post-processing over whatever raw silence intervals ffmpeg already
reported (see `split_video.segments.compute_segments`). So a single ffmpeg
scan per distinct threshold, using a very small `d=`, catches every
candidate gap regardless of length — everything else is free.

`WaveformCache` has no such parameter to key on — the peak envelope and loudness curve
don't depend on anything user-tunable — so it only ever computes once per file.

`ClassificationCache` is populated by an explicit background job (see
`job_work.analysis_work`) rather than lazily on first
request — inference over a long recording takes real time, so it's opt-in
via the editor's "Analyze audio" action. Its `rethreshold` re-derives
regions from the cached per-frame bucket scores, so retuning a threshold
doesn't require rerunning ffmpeg or the model.

All three also persist what they compute through the `MediaStore` (locally a
sidecar cache file next to the source), so a later `edit` of the same file loads
straight from disk instead of re-decoding audio or rerunning inference.
That's purely a startup-time shortcut: `__init__` seeds in-memory state
from any matching on-disk cache, and each real compute saves back, but the
live-session behavior (re-thresholding, recompute-on-slider-change) is
otherwise unchanged.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import closing

from split_video.editor import classify
from split_video.editor.classify import ClassificationRegion
from split_video.ffmpeg import WAVEFORM_SAMPLE_RATE, ProbeError, detect_silence, iter_pcm_chunks
from split_video.jobs import CancelToken
from split_video.media_store import MediaStore
from split_video.silence import SilenceInterval
from split_video.waveform import Loudness, WaveformPeaks, stream_waveform

RAW_SCAN_MIN_SILENCE_DURATION = 0.05


class SilenceCache:
    """Scoped to one `split-video edit` server process for one source (a media id)."""

    def __init__(self, media: MediaStore, source: str) -> None:
        self._media = media
        self._source = source
        self._lock = threading.Lock()
        self._raw: dict[float, list[SilenceInterval]] = self._load()

    def _load(self) -> dict[float, list[SilenceInterval]]:
        payload = self._media.load_cache(self._source, "silence")
        if payload is None:
            return {}
        return {
            float(threshold): [SilenceInterval(start=start, end=end) for start, end in intervals]
            for threshold, intervals in payload.items()
        }

    def _save(self) -> None:
        payload = {
            str(threshold): [[interval.start, interval.end] for interval in intervals]
            for threshold, intervals in self._raw.items()
        }
        self._media.save_cache(self._source, "silence", payload)

    def stored(self) -> dict[float, list[SilenceInterval]]:
        """The raw scans already stored, by threshold (dB). Never scans: empty if nothing was stored."""
        with self._lock:
            return {threshold: list(intervals) for threshold, intervals in self._raw.items()}

    def get_raw_silences(
        self,
        threshold: float,
        cancel: CancelToken | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> list[SilenceInterval]:
        """The raw scan at `threshold`, made (one ffmpeg pass) and stored if there is none yet. A cancelled `cancel`
        token stops the pass with `JobCancelledError` and stores nothing. `on_progress(seconds)` follows the pass."""
        with self._lock:
            if threshold in self._raw:
                return self._raw[threshold]
            with self._media.local_path(self._source) as path:
                extra = {"on_progress": on_progress} if on_progress is not None else {}
                intervals = detect_silence(
                    path, f"{threshold}dB", RAW_SCAN_MIN_SILENCE_DURATION, cancel=cancel, **extra
                )
            self._raw[threshold] = intervals
            self._save()
            return intervals


class WaveformCache:
    """Scoped to one `split-video edit` server process for one source (a media id).

    One streamed decode pass produces both the peak envelope and the loudness curve (see
    `waveform.stream_waveform`), and both are saved in the "waveform" cache entry. Entries written
    before the loudness curve existed load fine: the peaks are served as they are and the curve is
    computed (and the entry rewritten) the first time `get_loudness` is asked for."""

    def __init__(self, media: MediaStore, source: str) -> None:
        self._media = media
        self._source = source
        self._lock = threading.Lock()
        self._peaks: WaveformPeaks | None = None
        self._loudness: Loudness | None = None
        self._load()

    def _load(self) -> None:
        payload = self._media.load_cache(self._source, "waveform")
        if payload is None:
            return
        self._peaks = WaveformPeaks(buckets=[(lo, hi) for lo, hi in payload["buckets"]])
        loudness = payload.get("loudness")  # absent in entries from before SPL-41
        if loudness is not None:
            self._loudness = Loudness(window_seconds=loudness["window_seconds"], db=list(loudness["db"]))

    def _save(self) -> None:
        assert self._peaks is not None
        payload: dict = {"buckets": [list(bucket) for bucket in self._peaks.buckets]}
        if self._loudness is not None:
            payload["loudness"] = {"window_seconds": self._loudness.window_seconds, "db": self._loudness.db}
        self._media.save_cache(self._source, "waveform", payload)

    def _compute(self, cancel: CancelToken | None = None) -> None:
        try:
            expected: int | None = int(self._media.probe(self._source).duration * WAVEFORM_SAMPLE_RATE)
        except ProbeError:
            expected = None  # unknown length: the peaks are then kept raw (see waveform.PeakAccumulator)
        with self._media.local_path(self._source) as path, closing(iter_pcm_chunks(path, cancel=cancel)) as chunks:
            self._peaks, self._loudness = stream_waveform(chunks, WAVEFORM_SAMPLE_RATE, expected)
        self._save()

    def stored_loudness(self) -> Loudness | None:
        """The loudness curve if one is stored, else None. Never decodes audio (unlike `get_loudness`)."""
        with self._lock:
            return self._loudness

    def get_peaks(self) -> WaveformPeaks:
        with self._lock:
            if self._peaks is None:
                self._compute()
            assert self._peaks is not None
            return self._peaks

    def get_loudness(self, cancel: CancelToken | None = None) -> Loudness:
        """The loudness curve, decoding the audio once (streamed) to make and store it if there is none. A cancelled
        `cancel` token stops the decode with `JobCancelledError` and stores nothing."""
        with self._lock:
            if self._loudness is None:
                self._compute(cancel)
            assert self._loudness is not None
            return self._loudness


class ClassificationCache:
    """Scoped to one `split-video edit` server process for one source (a media id)."""

    def __init__(self, media: MediaStore, source: str) -> None:
        self._media = media
        self._source = source
        self._lock = threading.Lock()
        self._thresholds: dict[str, float] = dict(classify.DEFAULT_THRESHOLDS)
        self._frame_bucket_scores, self._total_duration = self._load()
        self._regions: list[ClassificationRegion] = (
            []
            if self._frame_bucket_scores is None
            else classify.rethreshold(self._frame_bucket_scores, self._thresholds, self._total_duration)
        )

    def _load(self) -> tuple[list[dict[str, float]] | None, float]:
        payload = self._media.load_cache(self._source, "classification")
        if payload is None:
            return None, 0.0
        buckets = payload["buckets"]
        frame_bucket_scores = [dict(zip(buckets, scores, strict=True)) for scores in payload["frame_scores"]]
        return frame_bucket_scores, payload["total_duration"]

    def _save(self) -> None:
        assert self._frame_bucket_scores is not None
        # Frames as parallel float arrays against one shared bucket-name
        # list, not a dict per frame: the six bucket names would otherwise
        # repeat in every one of a long recording's thousands of frames.
        buckets = list(self._frame_bucket_scores[0].keys()) if self._frame_bucket_scores else []
        payload = {
            "buckets": buckets,
            "frame_scores": [[scores[bucket] for bucket in buckets] for scores in self._frame_bucket_scores],
            "total_duration": self._total_duration,
        }
        self._media.save_cache(self._source, "classification", payload)

    @property
    def is_analyzed(self) -> bool:
        with self._lock:
            return self._frame_bucket_scores is not None

    @property
    def total_duration(self) -> float:
        """Seconds of audio that were classified (0.0 before an analysis)."""
        with self._lock:
            return float(self._total_duration)

    def get_thresholds(self) -> dict[str, float]:
        with self._lock:
            return dict(self._thresholds)

    def get_regions(self) -> list[ClassificationRegion]:
        with self._lock:
            return list(self._regions)

    def get_lanes(self) -> dict[str, list[ClassificationRegion]]:
        """Per-bucket independent presence spans for the timeline's detail
        view — cheap post-processing over the cached per-frame scores, like
        `rethreshold`, so it never touches ffmpeg or the model."""
        with self._lock:
            if self._frame_bucket_scores is None:
                return {bucket: [] for bucket in classify.LANE_BUCKETS}
            return classify.lane_regions(self._frame_bucket_scores, self._thresholds, self._total_duration)

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
            self._save()

    def rethreshold(self, thresholds: dict[str, float]) -> list[ClassificationRegion]:
        with self._lock:
            if self._frame_bucket_scores is None:
                raise RuntimeError("no analysis result to rethreshold yet")
            self._thresholds = dict(thresholds)
            self._regions = classify.rethreshold(self._frame_bucket_scores, self._thresholds, self._total_duration)
            return list(self._regions)
