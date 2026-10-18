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


# --- multiple tracks / free placement ------------------------------------------


def _with_overlay():
    p, tid, a, b = _two_clips()
    p, overlay = ops.add_track(p, "video")
    p, audio = ops.add_track(p, "audio")
    return p, tid, overlay.id, audio.id


def test_new_tracks_are_free_and_stack_in_order():
    p, main, overlay, audio = _with_overlay()
    assert [t.id for t in p.tracks] == [main, overlay, audio]
    assert [t.magnetic for t in p.tracks] == [True, False, False]
    with pytest.raises(TimelineError):
        ops.add_track(p, "text")


def test_free_track_places_clips_at_start_and_defaults_to_the_end():
    p, main, overlay, audio = _with_overlay()
    p, c1 = ops.add_clip(p, overlay, "o.mp4", 3.0, start=2.0)
    p, c2 = ops.add_clip(p, overlay, "o2.mp4", 3.0)
    assert (c1.start, c2.start) == (2.0, 5.0)
    assert p.duration == 18.0  # main track is still the longest


def test_free_track_rejects_overlap_and_negative_start():
    p, main, overlay, audio = _with_overlay()
    p, _ = ops.add_clip(p, overlay, "o.mp4", 4.0, start=1.0)
    with pytest.raises(TimelineError, match="overlap"):
        ops.add_clip(p, overlay, "x.mp4", 4.0, start=4.0)
    with pytest.raises(TimelineError):
        ops.add_clip(p, overlay, "x.mp4", 4.0, start=-1.0)
    ops.add_clip(p, overlay, "x.mp4", 4.0, start=5.0)  # touching is fine


def test_clips_overlap_freely_across_tracks():
    p, main, overlay, audio = _with_overlay()
    p, _ = ops.add_clip(p, overlay, "o.mp4", 4.0, start=1.0)
    p, _ = ops.add_clip(p, audio, "m.mp3", 30.0, start=0.0)
    assert p.duration == 30.0


def test_index_and_start_are_not_interchangeable():
    p, main, overlay, audio = _with_overlay()
    with pytest.raises(TimelineError):
        ops.add_clip(p, main, "x.mp4", 4.0, start=1.0)
    with pytest.raises(TimelineError):
        ops.add_clip(p, overlay, "x.mp4", 4.0, index=0)


def test_place_clip_moves_in_time_and_between_free_tracks():
    p, main, overlay, audio = _with_overlay()
    p, overlay2 = ops.add_track(p, "video")
    p, c = ops.add_clip(p, overlay, "o.mp4", 3.0, start=0.0)
    p = ops.place_clip(p, c.id, start=6.0)
    assert ops.find_clip_in(p, c.id).start == 6.0
    p = ops.place_clip(p, c.id, start=1.0, track_id=overlay2.id)
    track, _ = ops.find_clip(p, c.id)
    assert track.id == overlay2.id and not ops.find_track(p, overlay).clips


def test_place_clip_rejects_wrong_kind_main_track_and_overlap():
    p, main, overlay, audio = _with_overlay()
    p, c = ops.add_clip(p, overlay, "o.mp4", 3.0, start=0.0)
    p, d = ops.add_clip(p, overlay, "o2.mp4", 3.0, start=4.0)
    with pytest.raises(TimelineError, match="audio"):
        ops.place_clip(p, c.id, track_id=audio)
    with pytest.raises(TimelineError, match="main track"):
        ops.place_clip(p, c.id, track_id=main)
    with pytest.raises(TimelineError, match="overlap"):
        ops.place_clip(p, c.id, start=2.0)


def test_trim_left_edge_on_free_track_keeps_content_in_place():
    p, main, overlay, audio = _with_overlay()
    p, c = ops.add_clip(p, overlay, "o.mp4", 10.0, start=5.0)
    p = ops.trim_clip(p, c.id, 10.0, in_point=2.0)
    clip = ops.find_clip_in(p, c.id)
    assert (clip.start, clip.duration) == (7.0, 8.0)


def test_trim_longer_into_a_neighbour_is_rejected():
    p, main, overlay, audio = _with_overlay()
    p, c = ops.add_clip(p, overlay, "o.mp4", 10.0, out_point=3.0, start=0.0)
    p, _ = ops.add_clip(p, overlay, "n.mp4", 3.0, start=4.0)
    with pytest.raises(TimelineError, match="overlap"):
        ops.trim_clip(p, c.id, 10.0, out_point=6.0)


def test_move_clip_and_crossfade_are_main_track_only():
    p, main, overlay, audio = _with_overlay()
    p, c = ops.add_clip(p, overlay, "o.mp4", 3.0)
    with pytest.raises(TimelineError):
        ops.move_clip(p, c.id, 0)
    with pytest.raises(TimelineError, match="main track"):
        ops.set_transition(p, c.id, "crossfade", 1.0)


def test_remove_track_keeps_one_video_track():
    p, main, overlay, audio = _with_overlay()
    p = ops.remove_track(p, overlay)
    p = ops.remove_track(p, audio)
    with pytest.raises(TimelineError, match="at least one video"):
        ops.remove_track(p, main)
    with pytest.raises(TimelineError):
        ops.remove_track(p, "zzz")


def test_move_track_changes_stacking_order():
    p, main, overlay, audio = _with_overlay()
    p = ops.move_track(p, audio, 0)
    assert [t.id for t in p.tracks] == [audio, main, overlay]
    with pytest.raises(TimelineError):
        ops.move_track(p, audio, 5)


def test_set_clip_properties_updates_and_validates():
    p, tid, a, b = _two_clips()
    p = ops.set_clip_properties(p, a, gain=2.0, muted=True, x=0.25, scale=0.5, opacity=0.8, brightness=0.1)
    clip = ops.find_clip_in(p, a)
    assert (clip.gain, clip.muted, clip.x, clip.scale, clip.opacity, clip.brightness) == (
        2.0,
        True,
        0.25,
        0.5,
        0.8,
        0.1,
    )
    assert clip.start == 0.0 and ops.find_clip_in(p, b).start == 10.0  # layout untouched
    for bad in ({"opacity": 1.5}, {"gain": -1}, {"scale": 0}, {"colour": 1}, {"x": True}, {}):
        with pytest.raises(TimelineError):
            ops.set_clip_properties(p, a, **bad)
    with pytest.raises(TimelineError):
        ops.set_clip_properties(p, "zzz", gain=1.0)
