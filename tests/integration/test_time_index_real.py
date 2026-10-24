"""One real end to end check of the time index (SPL-100): 20 s of video whose screen shows known text (a cut at 10 s)
with LibriSpeech speech over the first 11 s (tests/fixtures/speech_clip.ogg), then digital silence. Every signal is
made by its real engine (YAMNet, ffmpeg scene detection, tesseract, faster-whisper, silencedetect, the loudness
pass), then `at` must return them all, consistently. Skipped without any of them (see test_search_real.py)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from screen_clip import mono_font

from split_video import assets, ocr, project, scenes, time_index, transcribe
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.local_media_store import LocalMediaStore

CLIP = Path(__file__).resolve().parents[1] / "fixtures" / "speech_clip.ogg"
FIRST = "FAILED tests/unit/test_welcome.py::test_welcome_his_gospel"  # 'gospel' is said at about 4.5 s
SECOND = "Chapter two silence detection"  # from 10 s

models_source = os.environ.get("SPLIT_VIDEO_TEST_MODELS")
pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(not transcribe.available(), reason="faster-whisper is not installed"),
    pytest.mark.skipif(
        not models_source or not (Path(models_source) / "models--Systran--faster-whisper-base.en").is_dir(),
        reason="set SPLIT_VIDEO_TEST_MODELS to a folder holding the base.en model",
    ),
    pytest.mark.skipif(not classify.MODEL_PATH.exists(), reason="needs the YAMNet model (make fetch-model)"),
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
    font = mono_font()

    def screen(color, text, fontcolor, label):
        text = text.replace(":", "\\:")
        draw = f"drawtext=fontfile={font}:text='{text}':fontcolor={fontcolor}:fontsize=34:x=60:y=300"
        return f"color=c={color}:s=1280x720:r=5:d=10,{draw}[{label}]"

    graph = ";".join(
        [screen("0x1e1e2e", FIRST, "white", "a"), screen("0xf5f5f0", SECOND, "0x202020", "b")]
        + ["[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]"]
    )
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(CLIP), "-filter_complex", graph, "-map", "[v]", "-map", "0:a"]
    cmd += ["-af", "apad", "-t", "20", "-c:v", "libx264", "-preset", "veryfast", "-g", "25", str(root / "talk.mp4")]
    done = subprocess.run(cmd, capture_output=True, check=False)
    assert done.returncode == 0, done.stderr.decode()[-500:]
    return root


def test_every_real_signal_at_a_time(workspace):
    media = LocalMediaStore(workspace)
    asset = assets.register_path(media, "talk.mp4")
    assert asset.duration == pytest.approx(20.0, abs=0.3)

    before = time_index.what_is_happening(media, asset.id, 5.0)  # nothing made yet: all hints, no error
    assert [m["signal"] for m in before["missing"]] == ["audio", "loudness", "silence", "speech", "scenes", "ocr"]

    cache = ClassificationCache(media, asset.path)
    project.run_analysis(media, asset.path, cache, cache.get_thresholds())
    SilenceCache(media, asset.path).get_raw_silences(-35.0)
    WaveformCache(media, asset.path).get_loudness()
    assert transcribe.transcribe(media, asset.id)["made"]
    assert scenes.analyze(media, asset.id)["scenes"] >= 1
    assert ocr.recognize(media, asset.id, interval=6)["ocr_frames"] >= 1

    index = time_index.TimeIndex.load(media, asset.id)
    assert index.missing == [] and all(v for k, v in index.available.items() if k != "description")

    talking = index.at(5.0, 6.0)  # [2, 8)
    assert talking["missing"] == [] and talking["window"] == {"start": 2.0, "end": 8.0}
    assert talking["audio"]["class"] in ("speech", "singing", "music") and talking["audio"]["regions"]
    assert talking["speech"]["word_count"] >= 8 and "gospel" in talking["speech"]["text"].lower()
    assert all(2.0 - 0.5 <= w["start"] < 8.0 for w in talking["speech"]["words"])
    assert talking["loudness"]["max_db"] > -50 and len(talking["loudness"]["curve"]) == 6
    assert talking["silence"]["inside"] is False
    assert "FAILED" in talking["visual"]["text"]["text"] and talking["visual"]["text"]["time"] == 0.0
    assert talking["visual"]["scene_index"] == 0 and talking["visual"]["scene_change"] is None
    assert talking["visual"]["shot_start"] == 0.0

    quiet = index.at(15.0, 6.0)  # [12, 18): the speech has ended; the second screen is up since the cut at 10 s
    assert quiet["speech"]["word_count"] == 0 and quiet["silence"]["inside"] is True
    assert quiet["audio"]["class"] == "silence_other" and quiet["loudness"]["max_db"] < -60
    assert SECOND.split()[0] in quiet["visual"]["text"]["text"] and quiet["visual"]["scene_index"] == 1
    assert quiet["visual"]["shot_start"] == pytest.approx(10.0, abs=0.5)

    cut = index.at(10.0, 4.0)  # the cut is inside the window
    assert cut["visual"]["scene_change"]["time"] == pytest.approx(10.0, abs=0.5) and cut["visual"]["scene_changes"] == 1
    assert cut["visual"]["activity"]["max"] > 0.5

    line = index.timeline(0, 20, 5)
    assert [p["start"] for p in line["points"]] == [0.0, 5.0, 10.0, 15.0]
    assert line["points"][0]["word_count"] > 0 and line["points"][3]["word_count"] == 0
    assert "FAILED" in line["points"][0]["text"] and line["points"][3]["text"].startswith("Chapter")
