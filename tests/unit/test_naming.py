from datetime import datetime, timezone
from pathlib import Path

from split_video.naming import build_manifest, segment_filename
from split_video.segments import Segment


def test_segment_filename_pads_to_two_digits_by_default():
    assert segment_filename(1, total=3, basename="liveshow", ext=".mp4") == "01 - liveshow.mp4"
    assert segment_filename(3, total=3, basename="liveshow", ext=".mp4") == "03 - liveshow.mp4"


def test_segment_filename_widens_past_ninety_nine_segments():
    assert segment_filename(5, total=120, basename="liveshow", ext=".mp4") == "005 - liveshow.mp4"


def test_build_manifest_shape():
    segments = [Segment(index=1, start=0.0, end=10.0), Segment(index=2, start=10.0, end=25.5)]
    filenames = ["01 - show.mp4", "02 - show.mp4"]
    manifest = build_manifest(
        source_path=Path("show.mp4"),
        segments=segments,
        filenames=filenames,
        parameters={"silence_threshold": "-35dB"},
        generated_at=datetime(2026, 8, 22, 18, 30, tzinfo=timezone.utc),
    )

    assert manifest["source_file"] == "show.mp4"
    assert manifest["parameters"] == {"silence_threshold": "-35dB"}
    assert manifest["segments"] == [
        {"index": 1, "file": "01 - show.mp4", "start": 0.0, "end": 10.0, "duration": 10.0},
        {"index": 2, "file": "02 - show.mp4", "start": 10.0, "end": 25.5, "duration": 15.5},
    ]
