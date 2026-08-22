"""Per-process cache of raw silencedetect output, keyed by threshold.

Only `silence_threshold` requires a real ffmpeg pass: it's baked into the
decode-time audio filter. `min_silence_duration`, `min_song_length`, and
`padding` are all pure post-processing over whatever raw silence intervals
ffmpeg already reported (see `split_video.segments.compute_segments`). So a
single ffmpeg scan per distinct threshold, using a very small `d=`, catches
every candidate gap regardless of length — everything else is free.
"""

from __future__ import annotations

import threading
from pathlib import Path

from split_video.ffmpeg import detect_silence
from split_video.silence import SilenceInterval

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
