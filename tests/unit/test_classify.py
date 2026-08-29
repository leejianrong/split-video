import numpy as np
import pytest

from split_video.editor.classify import (
    BUCKET_LABELS,
    CLASS_MAP_PATH,
    DEFAULT_THRESHOLDS,
    LANE_BUCKETS,
    SILENCE_BUCKET,
    ClassLabels,
    bucket_scores_for_frame,
    lane_regions,
    load_labels,
    merge_regions,
    rethreshold,
    winning_bucket,
)

LABELS = ClassLabels(
    display_names=["Music", "Speech", "Applause", "Noise"],
    bucket_indices={"music": [0], "speech": [1], "applause_crowd": [2], SILENCE_BUCKET: [3]},
)
THRESHOLDS = {"music": 0.3, "speech": 0.3, "applause_crowd": 0.3, SILENCE_BUCKET: 0.3}


def test_bucket_scores_for_frame_takes_max_within_each_bucket():
    scores = np.array([0.9, 0.1, 0.05, 0.02])
    assert bucket_scores_for_frame(scores, LABELS) == {
        "music": 0.9,
        "speech": 0.1,
        "applause_crowd": 0.05,
        SILENCE_BUCKET: 0.02,
    }


def test_winning_bucket_picks_highest_scoring_bucket_above_threshold():
    bucket, score = winning_bucket({"music": 0.9, "speech": 0.4, "applause_crowd": 0.1}, THRESHOLDS)
    assert (bucket, score) == ("music", 0.9)


def test_winning_bucket_ignores_buckets_below_their_threshold():
    bucket, score = winning_bucket({"music": 0.9, "speech": 0.95}, {"music": 0.99, "speech": 0.3})
    assert (bucket, score) == ("speech", 0.95)


def test_winning_bucket_falls_back_to_silence_when_nothing_clears_threshold():
    bucket, score = winning_bucket(
        {"music": 0.1, "speech": 0.05, SILENCE_BUCKET: 0.2}, {"music": 0.3, "speech": 0.3, SILENCE_BUCKET: 0.3}
    )
    assert (bucket, score) == (SILENCE_BUCKET, 0.2)


def test_merge_regions_combines_consecutive_contiguous_same_bucket_frames():
    frames = [
        (0.0, 0.5, "music", 0.6, {"music": 0.6, "speech": 0.05}),
        (0.5, 1.0, "music", 0.8, {"music": 0.8, "speech": 0.05}),
        (1.0, 1.5, "speech", 0.7, {"music": 0.05, "speech": 0.7}),
    ]
    regions = merge_regions(frames, THRESHOLDS)
    assert [(r.start, r.end, r.bucket, r.score) for r in regions] == [
        (0.0, 1.0, "music", 0.8),
        (1.0, 1.5, "speech", 0.7),
    ]


def test_merge_regions_does_not_merge_across_a_gap_even_with_same_bucket():
    frames = [
        (0.0, 0.5, "music", 0.6, {"music": 0.6}),
        (0.6, 1.0, "music", 0.8, {"music": 0.8}),
    ]
    regions = merge_regions(frames, THRESHOLDS)
    assert len(regions) == 2


def test_merge_regions_empty_input_returns_no_regions():
    assert merge_regions([], THRESHOLDS) == []


def test_merge_regions_reports_secondary_bucket_that_also_clears_its_threshold():
    frames = [(0.0, 0.5, "music", 0.9, {"music": 0.9, "speech": 0.4, "applause_crowd": 0.1, SILENCE_BUCKET: 0.02})]
    regions = merge_regions(frames, THRESHOLDS)
    assert regions[0].secondary == "speech"
    assert regions[0].bucket_scores == {"music": 0.9, "speech": 0.4, "applause_crowd": 0.1, SILENCE_BUCKET: 0.02}


def test_merge_regions_secondary_is_none_when_nothing_else_clears_threshold():
    frames = [(0.0, 0.5, "music", 0.9, {"music": 0.9, "speech": 0.1, "applause_crowd": 0.05, SILENCE_BUCKET: 0.02})]
    regions = merge_regions(frames, THRESHOLDS)
    assert regions[0].secondary is None


def test_merge_regions_secondary_never_picks_the_silence_bucket():
    frames = [(0.0, 0.5, "music", 0.9, {"music": 0.9, "speech": 0.1, "applause_crowd": 0.05, SILENCE_BUCKET: 0.5})]
    regions = merge_regions(frames, THRESHOLDS)
    assert regions[0].secondary is None


def test_merge_regions_secondary_and_bucket_scores_track_the_max_across_merged_frames():
    frames = [
        (0.0, 0.5, "music", 0.9, {"music": 0.9, "speech": 0.1, "applause_crowd": 0.05, SILENCE_BUCKET: 0.02}),
        (0.5, 1.0, "music", 0.85, {"music": 0.85, "speech": 0.6, "applause_crowd": 0.05, SILENCE_BUCKET: 0.02}),
    ]
    regions = merge_regions(frames, THRESHOLDS)
    assert len(regions) == 1
    assert regions[0].secondary == "speech"
    assert regions[0].bucket_scores["speech"] == 0.6


def test_rethreshold_recomputes_regions_from_cached_bucket_scores_without_rerunning_inference():
    frame_bucket_scores = [
        {"music": 0.9, "speech": 0.1, "applause_crowd": 0.05, SILENCE_BUCKET: 0.02},
        {"music": 0.85, "speech": 0.1, "applause_crowd": 0.05, SILENCE_BUCKET: 0.02},
        {"music": 0.1, "speech": 0.9, "applause_crowd": 0.05, SILENCE_BUCKET: 0.02},
    ]
    # HOP_SAMPLES / SAMPLE_RATE = 7680 / 16000 = 0.48s per frame slot
    total_duration = 3 * 0.48
    regions = rethreshold(frame_bucket_scores, THRESHOLDS, total_duration)
    assert [(round(r.start, 2), round(r.end, 2), r.bucket) for r in regions] == [
        (0.0, 0.96, "music"),
        (0.96, 1.44, "speech"),
    ]

    # A stricter music threshold should flip those frames to the fallback bucket.
    stricter = {**THRESHOLDS, "music": 0.95}
    regions = rethreshold(frame_bucket_scores, stricter, total_duration)
    assert [r.bucket for r in regions] == [SILENCE_BUCKET, "speech"]


def test_lane_regions_shows_independently_overlapping_buckets():
    frame_bucket_scores = [
        {"music": 0.9, "singing": 0.8, "speech": 0.05, "applause_crowd": 0.02, "laughter": 0.01, SILENCE_BUCKET: 0.02},
        {
            "music": 0.85,
            "singing": 0.75,
            "speech": 0.05,
            "applause_crowd": 0.02,
            "laughter": 0.01,
            SILENCE_BUCKET: 0.02,
        },
    ]
    total_duration = 2 * 0.48
    lanes = lane_regions(frame_bucket_scores, DEFAULT_THRESHOLDS, total_duration)

    assert set(lanes) == set(LANE_BUCKETS)
    assert [(round(r.start, 2), round(r.end, 2)) for r in lanes["music"]] == [(0.0, 0.96)]
    assert [(round(r.start, 2), round(r.end, 2)) for r in lanes["singing"]] == [(0.0, 0.96)]
    # Neither frame clears speech's own threshold, so its lane stays empty
    # even though music "won" every frame.
    assert lanes["speech"] == []


def test_lane_regions_excludes_the_silence_bucket():
    lanes = lane_regions([], DEFAULT_THRESHOLDS, 0.0)
    assert SILENCE_BUCKET not in lanes


def test_lane_regions_spans_never_set_a_secondary_bucket():
    frame_bucket_scores = [
        {"music": 0.9, "singing": 0.1, "speech": 0.05, "applause_crowd": 0.02, "laughter": 0.01, SILENCE_BUCKET: 0.02}
    ]
    lanes = lane_regions(frame_bucket_scores, DEFAULT_THRESHOLDS, 0.48)
    assert lanes["music"][0].secondary is None


@pytest.mark.skipif(not CLASS_MAP_PATH.exists(), reason="run `make fetch-model` first")
def test_every_bucket_label_is_a_real_audioset_class_name():
    # Guards against re-introducing labels that sound plausible but aren't
    # actually in AudioSet's 521-class ontology (this happened once already:
    # "Male speech, man speaking", "Female speech, woman speaking", "Booing",
    # and "Background noise" were all fabricated-sounding names that don't
    # exist — load_labels would silently KeyError on any of these).
    labels = load_labels()
    for bucket, names in BUCKET_LABELS.items():
        for name in names:
            assert name in labels.display_names, f"{name!r} (bucket {bucket!r}) is not a real AudioSet class"
