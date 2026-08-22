"""Parsing of ffmpeg's silencedetect filter output."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass(frozen=True)
class SilenceInterval:
    start: float
    end: float


def parse_silencedetect_output(text: str, total_duration: float) -> list[SilenceInterval]:
    """Parse ffmpeg stderr produced by the silencedetect filter.

    A silence_start with no matching silence_end means the file ended while
    still silent; that's treated as an implicit end at total_duration.
    """
    intervals: list[SilenceInterval] = []
    pending_start: float | None = None

    for line in text.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        if start_match is not None:
            pending_start = float(start_match.group(1))
            continue

        end_match = _SILENCE_END_RE.search(line)
        if end_match is not None and pending_start is not None:
            intervals.append(SilenceInterval(start=pending_start, end=float(end_match.group(1))))
            pending_start = None

    if pending_start is not None:
        intervals.append(SilenceInterval(start=pending_start, end=total_duration))

    return intervals
