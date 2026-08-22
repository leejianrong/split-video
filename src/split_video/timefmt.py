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
