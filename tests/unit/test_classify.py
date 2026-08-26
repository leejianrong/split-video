import numpy as np
import pytest

from split_video.editor.classify import (
    BUCKET_LABELS,
    CLASS_MAP_PATH,
    SILENCE_BUCKET,
    ClassLabels,
    bucket_scores_for_frame,
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
    frames = [(0.0, 0.5, "music", 0.6), (0.5, 1.0, "music", 0.8), (1.0, 1.5, "speech", 0.7)]
    regions = merge_regions(frames)
    assert [(r.start, r.end, r.bucket, r.score) for r in regions] == [
        (0.0, 1.0, "music", 0.8),
        (1.0, 1.5, "speech", 0.7),
    ]


def test_merge_regions_does_not_merge_across_a_gap_even_with_same_bucket():
    frames = [(0.0, 0.5, "music", 0.6), (0.6, 1.0, "music", 0.8)]
    regions = merge_regions(frames)
    assert len(regions) == 2


def test_merge_regions_empty_input_returns_no_regions():
    assert merge_regions([]) == []


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
