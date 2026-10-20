"""One real end to end check of the media report (SPL-44): the 20 s clip of test_time_index_real.py (screen text with a
cut at 10 s, LibriSpeech speech over the first 11 s, then digital silence). `prepare` makes loudness, silence and
the audio classes; transcription, scenes and OCR are run by their real engines (never by `prepare`); then the report
must show them all, consistently. Skipped without them (see test_time_index_real.py)."""

import pytest
from test_time_index_real import (  # noqa: F401  (the clip fixture and the skip marks)
    SECOND,
    pytestmark,
    workspace,
)

from split_video import assets, media_report, ocr, scenes, transcribe
from split_video.local_media_store import LocalMediaStore


def test_every_real_signal_in_one_report(workspace):  # noqa: F811
    media = LocalMediaStore(workspace)
    asset = assets.register_path(media, "talk.mp4")

    before = media_report.media_report(media, asset.id, bucket_seconds=5)
    assert [m["signal"] for m in before["signals"]["missing"]] == [
        "audio",
        "loudness",
        "silence",
        "speech",
        "scenes",
        "ocr",
    ]

    prepared = media_report.media_report(media, asset.id, bucket_seconds=5, prepare=True)["prepared"]
    assert prepared["prepared"] == ["loudness", "silence", "classification"] and prepared["skipped"] == []
    after = media_report.media_report(media, asset.id, bucket_seconds=5)
    assert [m["signal"] for m in after["signals"]["missing"]] == ["speech", "scenes", "ocr"]  # never automatic

    assert transcribe.transcribe(media, asset.id)["made"]
    assert scenes.analyze(media, asset.id)["scenes"] >= 1
    assert ocr.recognize(media, asset.id, interval=6)["ocr_frames"] >= 1

    doc = media_report.media_report(media, asset.id, bucket_seconds=5)
    assert doc["signals"]["missing"] == [] and all(doc["signals"]["available"].values())
    b = doc["buckets"]
    assert b["count"] == 4 and b["seconds"] == 5.0
    assert b["words"][0] >= 8 and b["transcript"][0] and b["words"][3] == 0
    assert b["loudness_max_db"][0] > -50 and b["loudness_max_db"][3] < -60
    assert b["silence_fraction"][3] > 0.8 and b["silence_fraction"][0] < 0.5  # a pause or two in the speech
    assert b["scene_changes"][1] + b["scene_changes"][2] >= 1  # the cut at about 10 s
    assert b["screen_text_changed"][0] == 1 and "FAILED" in b["screen_text"][0]
    assert b["screen_text_changed"][2] == 1 and SECOND.split()[0] in b["screen_text"][2]
    assert doc["transcript"]["words"] >= 8 and doc["transcript"]["language"] == "en"
    assert doc["silence_gaps"]["count"] >= 1 and doc["loudness"]["max_db"] > -50 and doc["regions"]["count"] >= 0
    assert doc["scene_changes"]["items"][0][0] == pytest.approx(10.0, abs=0.5)
    assert media_report.encoded_size(doc) < 5000
