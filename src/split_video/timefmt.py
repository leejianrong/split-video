"""Human-readable timestamp formatting for CLI display."""

from __future__ import annotations


def format_timestamp(seconds: float, total_duration: float) -> str:
    """Format a timestamp as M:SS, or H:MM:SS once the file is an hour or longer.

    `total_duration` (the whole file's length) decides the format so every
    row in a table uses the same width, rather than switching per-value.
    """
    whole_seconds = round(seconds)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if total_duration >= 3600:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def parse_time(text: str) -> float:
    """Parse seconds ("83.5") or a clock time ("1:23.5", "1:02:03.5") to seconds."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"not a time: {text!r}")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"not a time: {text!r}") from None
    if any(v < 0 for v in values):
        raise ValueError(f"time can't be negative: {text!r}")
    seconds = 0.0
    for v in values:
        seconds = seconds * 60 + v
    return seconds
