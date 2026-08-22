from split_video.segments import compute_segments
from split_video.silence import SilenceInterval


def _seg_tuples(segments):
    return [(round(s.start, 3), round(s.end, 3)) for s in segments]


def test_no_silence_keeps_whole_file_as_one_segment():
    segments = compute_segments([], total_duration=100.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 100.0)]


def test_three_songs_with_clean_gaps():
    silences = [SilenceInterval(50.0, 53.0), SilenceInterval(110.0, 114.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 51.5), (51.5, 112.0), (112.0, 200.0)]


def test_leading_silence_is_trimmed_not_a_phantom_segment():
    silences = [SilenceInterval(0.0, 3.0), SilenceInterval(100.0, 103.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(3.0, 101.5), (101.5, 200.0)]


def test_trailing_silence_is_trimmed_not_a_phantom_segment():
    silences = [SilenceInterval(100.0, 103.0), SilenceInterval(197.0, 200.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 101.5), (101.5, 197.0)]


def test_leading_and_trailing_silence_both_trimmed():
    silences = [SilenceInterval(0.0, 2.0), SilenceInterval(50.0, 53.0), SilenceInterval(198.0, 200.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(2.0, 51.5), (51.5, 198.0)]


def test_short_segment_merges_forward_into_next_song():
    # A false split inside a song: 20s "segment" is below min_song_length and
    # should be swallowed by the song that follows it.
    silences = [SilenceInterval(20.0, 23.0), SilenceInterval(40.0, 43.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 41.5), (41.5, 200.0)]


def test_short_final_segment_merges_backward_into_previous_song():
    silences = [SilenceInterval(100.0, 103.0), SilenceInterval(190.0, 193.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 101.5), (101.5, 200.0)]


def test_chained_merges_collapse_multiple_short_segments():
    # Three short candidate segments in a row (each < min_song_length) should
    # keep merging forward until the accumulated segment clears the minimum.
    silences = [
        SilenceInterval(10.0, 13.0),
        SilenceInterval(20.0, 23.0),
        SilenceInterval(30.0, 33.0),
    ]
    segments = compute_segments(silences, total_duration=100.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 31.5), (31.5, 100.0)]


def test_silences_shorter_than_min_silence_duration_are_ignored():
    silences = [SilenceInterval(50.0, 50.5)]  # 0.5s, below the 2.0s threshold
    segments = compute_segments(silences, total_duration=100.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 100.0)]


def test_cut_point_is_midpoint_clamped_by_edge_margin():
    # Gap is long enough that the midpoint sits well inside the margin.
    silences = [SilenceInterval(50.0, 60.0)]
    segments = compute_segments(
        silences, total_duration=100.0, min_silence_duration=2.0, min_song_length=10.0, edge_margin=0.3
    )
    assert _seg_tuples(segments) == [(0.0, 55.0), (55.0, 100.0)]


def test_trailing_silence_ending_exactly_at_total_duration():
    silences = [SilenceInterval(50.0, 53.0), SilenceInterval(190.0, 200.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 51.5), (51.5, 190.0)]
