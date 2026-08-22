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
    assert _seg_tuples(segments) == [(0.0, 50.15), (52.85, 110.15), (113.85, 200.0)]


def test_leading_silence_is_trimmed_not_a_phantom_segment():
    silences = [SilenceInterval(0.0, 3.0), SilenceInterval(100.0, 103.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(2.85, 100.15), (102.85, 200.0)]


def test_trailing_silence_is_trimmed_not_a_phantom_segment():
    silences = [SilenceInterval(100.0, 103.0), SilenceInterval(197.0, 200.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 100.15), (102.85, 197.15)]


def test_leading_and_trailing_silence_both_trimmed():
    silences = [SilenceInterval(0.0, 2.0), SilenceInterval(50.0, 53.0), SilenceInterval(198.0, 200.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(1.85, 50.15), (52.85, 198.15)]


def test_short_segment_merges_forward_into_next_song():
    # A false split inside a song: the candidate segment is below
    # min_song_length and should be swallowed by the song that follows it.
    silences = [SilenceInterval(20.0, 23.0), SilenceInterval(40.0, 43.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 40.15), (42.85, 200.0)]


def test_short_final_segment_merges_backward_into_previous_song():
    silences = [SilenceInterval(100.0, 103.0), SilenceInterval(190.0, 193.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 100.15), (102.85, 200.0)]


def test_chained_merges_collapse_multiple_short_segments():
    # Three short candidate segments in a row (each < min_song_length) should
    # keep merging forward until the accumulated segment clears the minimum.
    silences = [
        SilenceInterval(10.0, 13.0),
        SilenceInterval(20.0, 23.0),
        SilenceInterval(30.0, 33.0),
    ]
    segments = compute_segments(silences, total_duration=100.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 30.15), (32.85, 100.0)]


def test_silences_shorter_than_min_silence_duration_are_ignored():
    silences = [SilenceInterval(50.0, 50.5)]  # 0.5s, below the 2.0s threshold
    segments = compute_segments(silences, total_duration=100.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 100.0)]


def test_padded_cut_trims_gap_leaving_a_protective_sliver_on_each_side():
    silences = [SilenceInterval(50.0, 60.0)]
    segments = compute_segments(
        silences, total_duration=100.0, min_silence_duration=2.0, min_song_length=10.0, padding=0.3
    )
    assert _seg_tuples(segments) == [(0.0, 50.3), (59.7, 100.0)]


def test_degenerate_short_gap_collapses_padded_cuts_to_a_single_midpoint():
    # Gap (0.4s) is shorter than 2*padding (0.6s), so there's no room to pad
    # both sides without the two cuts crossing — falls back to one midpoint,
    # i.e. zero silence retained rather than a nonsensical negative-length gap.
    silences = [SilenceInterval(50.0, 50.4)]
    segments = compute_segments(
        silences, total_duration=100.0, min_silence_duration=0.3, min_song_length=5.0, padding=0.3
    )
    assert _seg_tuples(segments) == [(0.0, 50.2), (50.2, 100.0)]


def test_trailing_silence_ending_exactly_at_total_duration():
    silences = [SilenceInterval(50.0, 53.0), SilenceInterval(190.0, 200.0)]
    segments = compute_segments(silences, total_duration=200.0, min_silence_duration=2.0, min_song_length=30.0)
    assert _seg_tuples(segments) == [(0.0, 50.15), (52.85, 190.15)]
