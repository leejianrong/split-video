"""Reviewing highlight suggestions and making them into clips (SPL-64), on the in-memory and the local store."""

import pytest
from test_edit_plan import World

from split_video import assets, edit_ops, highlight_review, project
from split_video.errors import RevisionConflictError
from split_video.highlight_review import ReviewError, key_of


@pytest.fixture(params=["memory", "local"])
def world(request, tmp_path):
    return World(request.param, tmp_path)


def video_of(w):
    return assets.get_asset(w.media, w.ids["a"]).path


def test_key_is_the_scored_window_to_the_millisecond():
    assert key_of(250.0, 298.5) == "h_250000_298500"


def test_reviews_start_empty_and_persist(world):
    w, a = world, world.ids["a"]
    assert highlight_review.load(w.media, a) == {"version": 1, "revision": 0, "reviews": {}}
    doc = highlight_review.set_review(w.media, a, key_of(10, 40), "dismissed", start=10, end=40)
    assert doc["revision"] == 1 and doc["reviews"][key_of(10, 40)]["state"] == "dismissed"
    assert highlight_review.load(w.media, a) == doc  # stored, not just returned
    doc = highlight_review.set_review(
        w.media, a, key_of(10, 40), "accepted", start=10, end=40, adjusted_start=12, adjusted_end=38
    )
    assert doc["reviews"][key_of(10, 40)]["adjusted"] == {"start": 12.0, "end": 38.0}
    doc = highlight_review.set_review(w.media, a, key_of(10, 40), None, start=10, end=40)
    assert doc["reviews"] == {} and doc["revision"] == 3
    # an unreviewed suggestion the user only trimmed is kept as pending; pending at its own edges is no entry
    doc = highlight_review.set_review(
        w.media, a, key_of(10, 40), "pending", start=10, end=40, adjusted_start=11, adjusted_end=39
    )
    assert doc["reviews"][key_of(10, 40)] == {
        "state": "pending",
        "start": 10.0,
        "end": 40.0,
        "adjusted": {"start": 11.0, "end": 39.0},
    }
    doc = highlight_review.set_review(w.media, a, key_of(10, 40), "pending", start=10, end=40)
    assert doc["reviews"] == {}


def test_review_writes_check_the_revision(world):
    w, a = world, world.ids["a"]
    highlight_review.set_review(w.media, a, key_of(10, 40), "accepted", start=10, end=40)
    with pytest.raises(RevisionConflictError) as caught:
        highlight_review.set_review(w.media, a, key_of(50, 80), "accepted", start=50, end=80, base_revision=0)
    assert caught.value.current == 1
    assert list(highlight_review.load(w.media, a)["reviews"]) == [key_of(10, 40)]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"state": "maybe", "start": 10, "end": 40},
        {"state": "accepted", "start": 40, "end": 10},
        {"state": "accepted", "start": 10, "end": 10.5},
        {"state": "accepted", "start": -1, "end": 10},
        {"state": "accepted", "start": 10, "end": 9999},
        {"state": "accepted", "start": 10, "end": 40, "adjusted_start": 12},
    ],
)
def test_bad_reviews_are_refused(world, kwargs):
    start, end = kwargs["start"], kwargs["end"]
    with pytest.raises(ReviewError):
        highlight_review.set_review(world.media, world.ids["a"], key_of(start, end), **kwargs)
    assert highlight_review.load(world.media, world.ids["a"])["revision"] == 0


def test_a_key_that_is_not_the_window_is_refused(world):
    with pytest.raises(ReviewError, match="does not match"):
        highlight_review.set_review(world.media, world.ids["a"], "h_1_2", "accepted", start=10, end=40)


def test_annotate_marks_candidates_and_uses_adjusted_edges():
    suggestions = {"candidates": [{"start": 10.0, "end": 40.0, "score": 0.9}, {"start": 50.0, "end": 80.0}]}
    doc = {
        "version": 1,
        "revision": 1,
        "reviews": {
            key_of(10, 40): {"state": "accepted", "start": 10.0, "end": 40.0, "adjusted": {"start": 12.0, "end": 38.0}}
        },
    }
    first, second = highlight_review.annotate(suggestions, doc)
    assert first["key"] == "h_10000_40000" and first["review"] == {"state": "accepted", "start": 12.0, "end": 38.0}
    assert second["review"] == {"state": "pending", "start": 50.0, "end": 80.0}


def test_make_short_adds_cropped_labelled_clips_and_accepts_them(world):
    w, a, video = world, world.ids["a"], video_of(world)
    before = project.current_revision(w.store, video)
    items = [
        {"start": 10, "end": 40, "title": "The opener"},
        {"start": 20, "end": 50, "title": "", "scored": {"start": 21, "end": 51}},
    ]
    out = highlight_review.make_clips(w.media, w.store, video, a, items, "short", base_revision=before)
    assert out["revision"] == before + 1 and out["applied"] == 3 + 1  # two add_clip, two crops: 4 ops
    timeline = project.video_timeline(w.store, w.media, video)
    clips = timeline.tracks[0].clips
    shorts = [c for c in clips if c.label.startswith("Short: ")]
    assert [c.label for c in shorts] == ["Short: The opener", "Short: Highlight 2"]
    assert [(c.in_point, c.out_point) for c in shorts] == [(10, 40), (20, 50)]
    # a 640x360 recording cropped to 9:16: full height, 202.5 / 640 of the width, centred
    assert shorts[0].cropped and shorts[0].crop_h == 1.0 and round(shorts[0].crop_w, 4) == round(360 * 9 / 16 / 640, 4)
    reviews = highlight_review.load(w.media, a)["reviews"]
    assert reviews[key_of(10, 40)]["state"] == "accepted"
    assert reviews[key_of(21, 51)]["adjusted"] == {"start": 20.0, "end": 50.0}  # edges differ from the scored window
    assert [m["label"] for m in out["made"]] == ["Short: The opener", "Short: Highlight 2"]


def test_add_to_timeline_keeps_the_whole_frame(world):
    w, video = world, video_of(world)
    highlight_review.make_clips(w.media, w.store, video, w.ids["a"], [{"start": 5, "end": 25, "title": "Chat"}], "clip")
    clip = next(c for c in project.video_timeline(w.store, w.media, video).tracks[0].clips if c.label == "Chat")
    assert not clip.cropped


def test_making_twice_does_not_collide_on_ids(world):
    w, video, a = world, video_of(world), world.ids["a"]
    for _ in range(2):
        highlight_review.make_clips(w.media, w.store, video, a, [{"start": 5, "end": 25}], "short")
    clips = project.video_timeline(w.store, w.media, video).tracks[0].clips
    assert len({c.id for c in clips}) == len(clips)


def test_a_stale_project_revision_changes_nothing(world):
    w, video, a = world, video_of(world), world.ids["a"]
    current = project.current_revision(w.store, video)
    highlight_review.make_clips(w.media, w.store, video, a, [{"start": 5, "end": 25}], "short")
    with pytest.raises(RevisionConflictError):
        highlight_review.make_clips(
            w.media, w.store, video, a, [{"start": 30, "end": 50}], "short", base_revision=current
        )
    assert len(highlight_review.load(w.media, a)["reviews"]) == 1
    assert project.current_revision(w.store, video) == current + 1


def test_a_refused_batch_accepts_nothing(world):
    w, video, a = world, video_of(world), world.ids["a"]
    with pytest.raises((edit_ops.OpsInvalidError, ReviewError)):
        highlight_review.make_clips(w.media, w.store, video, a, [{"start": 5, "end": 25}, {"start": 1, "end": 9999}])
    assert highlight_review.load(w.media, a)["reviews"] == {}


def test_audio_only_asset_cannot_be_a_short(world):
    w = world
    with pytest.raises(ReviewError, match="no picture"):
        highlight_review.make_clips(w.media, w.store, video_of(w), w.ids["song"], [{"start": 5, "end": 25}], "short")


def test_nothing_to_make_is_refused(world):
    w = world
    with pytest.raises(ReviewError):
        highlight_review.make_clips(w.media, w.store, video_of(w), w.ids["a"], [], "short")
    with pytest.raises(ReviewError, match="kind"):
        highlight_review.make_clips(w.media, w.store, video_of(w), w.ids["a"], [{"start": 5, "end": 25}], "reel")
