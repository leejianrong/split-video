"""One real end to end check of the combined search (SPL-99): LibriSpeech speech (tests/fixtures/speech_clip.ogg,
see its LICENSE) muxed over a synthetic video whose screen shows known text, transcribed by the real faster-whisper
model and read by the real tesseract, then searched. Skipped without the transcribe extra and a model (see
tests/integration/test_transcribe_real.py), without tesseract, or without ffmpeg's drawtext and a monospace font."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from screen_clip import mono_font

from split_video import assets, ocr, search, transcribe
from split_video.local_media_store import LocalMediaStore

CLIP = Path(__file__).resolve().parents[1] / "fixtures" / "speech_clip.ogg"
SCREEN = "FAILED tests/unit/test_welcome.py::test_welcome_his_gospel"  # 'gospel' is said at about 4.5 s

models_source = os.environ.get("SPLIT_VIDEO_TEST_MODELS")
pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(not transcribe.available(), reason="faster-whisper is not installed"),
    pytest.mark.skipif(
        not models_source or not (Path(models_source) / "models--Systran--faster-whisper-base.en").is_dir(),
        reason="set SPLIT_VIDEO_TEST_MODELS to a folder holding the base.en model",
    ),
    pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs the tesseract program"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None or mono_font() is None, reason="needs ffmpeg and a font"),
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    root = tmp_path / "ws"
    root.mkdir()
    shutil.copytree(models_source, root / ".split-video-models")
    text = SCREEN.replace(":", "\\:")
    draw = f"drawtext=fontfile={mono_font()}:text='{text}':fontcolor=white:fontsize=34:x=60:y=300"
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x1e1e2e:s=1280x720:r=5", "-i", str(CLIP)]
    cmd += ["-vf", f"{draw},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-shortest"]
    cmd += [str(root / "talk.mp4")]
    done = subprocess.run(cmd, capture_output=True, check=False)
    assert done.returncode == 0, done.stderr.decode()[-500:]
    return root


def test_said_and_shown_is_one_moment_found_by_the_combined_search(workspace):
    media = LocalMediaStore(workspace)
    asset = assets.register_path(media, "talk.mp4")
    assert transcribe.transcribe(media, asset.id)["made"]
    assert ocr.recognize(media, asset.id, interval=6)["ocr_frames"] >= 1

    found = search.search_asset(media, asset.id, "gospel failed")
    assert found["sources"] == {"transcript": "searched", "ocr": "searched"}
    top = found["hits"][0]
    assert set(top["found_in"]) == {"transcript", "ocr"} and top["score"] > 0.7
    said = next(e for e in top["evidence"] if e["source"] == "transcript")
    shown = next(e for e in top["evidence"] if e["source"] == "ocr")
    assert "gospel" in said["text"].lower() and 4.0 < said["time"] < 6.0
    assert "FAILED" in shown["text"] and shown["time"] == 0.0
    assert top["time"] == 0.0 and top["end"] > said["end"]

    by_phrase = search.search_asset(media, asset.id, "welcome his gospel")
    assert set(by_phrase["hits"][0]["found_in"]) == {"transcript", "ocr"} and by_phrase["hits"][0]["match"] == "phrase"
    on_screen = search.search_asset(media, asset.id, "test_welcome_his_gospel")["hits"][0]
    assert on_screen["found_in"][0] == "ocr" and on_screen["time"] == 0.0  # its words are also said: corroborated
    only_said = search.search_asset(media, asset.id, "quilter", sources="transcript")
    assert (
        only_said["total_matches"] == 1
    )  # "quilter's" is another word and only_said["sources"]["ocr"] == "not requested"
