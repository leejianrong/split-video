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
    edge_margin: float = 0.3,
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

    song_start = leading.end if leading is not None else 0.0
    song_end = trailing.start if trailing is not None else total_duration

    boundaries: list[tuple[float, float]] = []
    cursor = song_start
    for gap in gaps:
        cut = _cut_point(gap, edge_margin)
        boundaries.append((cursor, cut))
        cursor = cut
    boundaries.append((cursor, song_end))

    boundaries = _merge_short_segments(boundaries, min_song_length)

    return [Segment(index=i + 1, start=start, end=end) for i, (start, end) in enumerate(boundaries)]


def _cut_point(gap: SilenceInterval, edge_margin: float) -> float:
    midpoint = (gap.start + gap.end) / 2
    low = gap.start + edge_margin
    high = gap.end - edge_margin
    if low > high:
        return midpoint
    return min(max(midpoint, low), high)


def _merge_short_segments(
    boundaries: list[tuple[float, float]], min_song_length: float
) -> list[tuple[float, float]]:
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
