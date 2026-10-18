import pytest

from split_video import timeline_ops as ops
from split_video.timeline import TimelineError, empty_project


def _two_clips(dur_a=10.0, dur_b=8.0):
    p = empty_project()
    tid = p.tracks[0].id
    p, a = ops.add_clip(p, tid, "a.mp4", dur_a)
    p, b = ops.add_clip(p, tid, "b.mp4", dur_b)
    return p, tid, a.id, b.id


def _clips(p):
    return list(p.tracks[0].clips)


def test_add_lays_clips_back_to_back():
    p, *_ = _two_clips()
    a, b = _clips(p)
    assert (a.start, a.end, b.start, b.end) == (0.0, 10.0, 10.0, 18.0)


def test_add_at_index_inserts_and_relayouts():
    p, tid, a, b = _two_clips()
    p, c = ops.add_clip(p, tid, "c.mp4", 4.0, index=1)
    assert [x.source for x in _clips(p)] == ["a.mp4", "c.mp4", "b.mp4"]
    assert [x.start for x in _clips(p)] == [0.0, 10.0, 14.0]


def test_add_validates_range_and_index():
    p = empty_project()
    tid = p.tracks[0].id
    with pytest.raises(TimelineError):
        ops.add_clip(p, tid, "a.mp4", 5.0, out_point=9.0)
    with pytest.raises(TimelineError):
        ops.add_clip(p, tid, "a.mp4", 5.0, in_point=4.95)
    with pytest.raises(TimelineError):
        ops.add_clip(p, tid, "a.mp4", 5.0, index=3)
    with pytest.raises(TimelineError):
        ops.add_clip(p, "nope", "a.mp4", 5.0)


def test_trim_shifts_following_clips():
    p, tid, a, b = _two_clips()
    p = ops.trim_clip(p, a, 10.0, in_point=2.0, out_point=7.0)
    first, second = _clips(p)
    assert first.duration == 5.0 and second.start == 5.0


def test_trim_rejects_bad_ranges():
    p, tid, a, b = _two_clips()
    for kwargs in ({"out_point": 11.0}, {"in_point": -1.0}, {"in_point": 9.95}, {"out_point": 0.05}):
        with pytest.raises(TimelineError):
            ops.trim_clip(p, a, 10.0, **kwargs)


def test_move_reorders_and_stays_gap_free():
    p, tid, a, b = _two_clips()
    p = ops.move_clip(p, b, 0)
    first, second = _clips(p)
    assert (first.id, second.id) == (b, a)
    assert (first.start, second.start) == (0.0, 8.0)
    with pytest.raises(TimelineError):
        ops.move_clip(p, b, 2)


def test_remove_closes_the_gap():
    p, tid, a, b = _two_clips()
    p = ops.remove_clip(p, a)
    assert [(c.id, c.start) for c in _clips(p)] == [(b, 0.0)]
    with pytest.raises(TimelineError):
        ops.remove_clip(p, a)


def test_crossfade_overlaps_and_shortens_total():
    p, tid, a, b = _two_clips()
    p = ops.set_transition(p, b, "crossfade", 2.0)
    second = _clips(p)[1]
    assert second.start == 8.0 and p.duration == 16.0


def test_crossfade_rejected_on_first_clip_or_too_long_or_zero():
    p, tid, a, b = _two_clips(dur_a=3.0, dur_b=8.0)
    with pytest.raises(TimelineError):
        ops.set_transition(p, a, "crossfade", 1.0)
    with pytest.raises(TimelineError):
        ops.set_transition(p, b, "crossfade", 3.5)
    with pytest.raises(TimelineError):
        ops.set_transition(p, b, "crossfade", 0.0)
    with pytest.raises(TimelineError):
        ops.set_transition(p, b, "wipe", 1.0)


def test_crossfades_on_both_sides_of_a_clip_must_fit_inside_it():
    p, tid, a, b = _two_clips(dur_a=10.0, dur_b=4.0)
    p, c = ops.add_clip(p, tid, "c.mp4", 10.0)
    p = ops.set_transition(p, b, "crossfade", 3.0)
    with pytest.raises(TimelineError):
        ops.set_transition(p, c.id, "crossfade", 2.0)  # clip b only has 1s left
    ops.set_transition(p, c.id, "crossfade", 1.0)


def test_reordering_resets_first_clips_transition_to_cut():
    p, tid, a, b = _two_clips()
    p = ops.set_transition(p, b, "crossfade", 2.0)
    p = ops.move_clip(p, b, 0)
    first = _clips(p)[0]
    assert first.transition == "cut" and first.transition_duration == 0.0


def test_trim_clamps_a_crossfade_that_no_longer_fits():
    p, tid, a, b = _two_clips()
    p = ops.set_transition(p, b, "crossfade", 5.0)
    p = ops.trim_clip(p, b, 8.0, in_point=0.0, out_point=3.0)
    assert _clips(p)[1].transition_duration == 3.0
    p = ops.trim_clip(p, a, 10.0, in_point=0.0, out_point=2.0)
    assert _clips(p)[1].transition_duration == 2.0


def test_set_cut_clears_duration():
    p, tid, a, b = _two_clips()
    p = ops.set_transition(p, b, "crossfade", 2.0)
    p = ops.set_transition(p, b, "cut")
    assert _clips(p)[1].transition_duration == 0.0 and p.duration == 18.0
