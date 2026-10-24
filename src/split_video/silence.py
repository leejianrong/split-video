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


class SilenceParser:
    """Incremental parser for the silencedetect filter's log lines. Feed it lines as ffmpeg writes
    them and keep only what it returns: memory is the parsed intervals, not the log."""

    def __init__(self) -> None:
        self.intervals: list[SilenceInterval] = []
        self.pending_start: float | None = None

    def feed(self, line: str) -> None:
        start_match = _SILENCE_START_RE.search(line)
        if start_match is not None:
            self.pending_start = float(start_match.group(1))
            return
        end_match = _SILENCE_END_RE.search(line)
        if end_match is not None and self.pending_start is not None:
            self.intervals.append(SilenceInterval(start=self.pending_start, end=float(end_match.group(1))))
            self.pending_start = None


def parse_silencedetect_output(text: str, total_duration: float) -> list[SilenceInterval]:
    """Parse ffmpeg stderr produced by the silencedetect filter.

    A silence_start with no matching silence_end means the file ended while
    still silent; that's treated as an implicit end at total_duration.
    """
    parser = SilenceParser()
    for line in text.splitlines():
        parser.feed(line)
    if parser.pending_start is not None:
        parser.intervals.append(SilenceInterval(start=parser.pending_start, end=total_duration))
    return parser.intervals
