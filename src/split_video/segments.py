"""Turn detected silence gaps into a list of song segments."""

from __future__ import annotations

from dataclasses import dataclass

from split_video.silence import SilenceInterval

# Silence touching either edge of the file within this tolerance is treated
# as dead air rather than a boundary between two songs.
_LEADING_TRAILING_EPSILON = 0.1


@dataclass(frozen=True)
class Segment:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def compute_segments(
    silences: list[SilenceInterval],
    total_duration: float,
    min_silence_duration: float,
    min_song_length: float,
    padding: float = 0.15,
) -> list[Segment]:
    # Defensive re-filter: ffmpeg's own `silencedetect=d=...` already only
    # reports gaps of at least min_silence_duration, but this keeps the
    # function testable without invoking ffmpeg.
    gaps = sorted(
        (s for s in silences if s.end - s.start >= min_silence_duration),
        key=lambda s: s.start,
    )

    leading = None
    if gaps and gaps[0].start <= _LEADING_TRAILING_EPSILON:
        leading = gaps.pop(0)

    trailing = None
    if gaps and gaps[-1].end >= total_duration - _LEADING_TRAILING_EPSILON:
        trailing = gaps.pop(-1)

    song_start = max(0.0, leading.end - padding) if leading is not None else 0.0
    song_end = min(total_duration, trailing.start + padding) if trailing is not None else total_duration

    boundaries: list[tuple[float, float]] = []
    cursor = song_start
    for gap in gaps:
        cut_out, cut_in = _cut_points(gap, padding)
        boundaries.append((cursor, cut_out))
        cursor = cut_in
    boundaries.append((cursor, song_end))

    boundaries = _merge_short_segments(boundaries, min_song_length)

    return [Segment(index=i + 1, start=start, end=end) for i, (start, end) in enumerate(boundaries)]


def _cut_points(gap: SilenceInterval, padding: float) -> tuple[float, float]:
    """Return (song_before_end, song_after_start) for an interior silence gap.

    Trims the gap out entirely, keeping only a small protective sliver of
    silence on each side so a quiet attack/decay transient isn't clipped.
    """
    if gap.end - gap.start <= 2 * padding:
        # Gap too short to pad both sides without the two cuts crossing;
        # degenerate to a single midpoint (zero retained silence).
        midpoint = (gap.start + gap.end) / 2
        return midpoint, midpoint
    return gap.start + padding, gap.end - padding


def _merge_short_segments(boundaries: list[tuple[float, float]], min_song_length: float) -> list[tuple[float, float]]:
    boundaries = list(boundaries)

    while len(boundaries) > 1:
        short_index = next(
            (i for i, (start, end) in enumerate(boundaries) if end - start < min_song_length),
            None,
        )
        if short_index is None:
            break

        if short_index < len(boundaries) - 1:
            # Merge forward: a short segment is almost always a false split
            # from a quiet passage inside the song that follows it.
            start, _ = boundaries[short_index]
            _, next_end = boundaries[short_index + 1]
            boundaries[short_index : short_index + 2] = [(start, next_end)]
        else:
            # No segment to merge into; the last segment merges backward.
            prev_start, _ = boundaries[short_index - 1]
            _, end = boundaries[short_index]
            boundaries[short_index - 1 : short_index + 1] = [(prev_start, end)]

    return boundaries
