"""Transcription with the real faster-whisper model on a real speech clip (LibriSpeech, see
tests/fixtures/speech_clip.LICENSE.txt).

These need the optional extra (`uv sync --extra transcribe`) and a downloaded model: set
SPLIT_VIDEO_TEST_MODELS to a directory holding `models--Systran--faster-whisper-base.en` (any faster-whisper
download_root, e.g. a workspace's `.split-video-models/`). Without both they are skipped, so the default CI run,
which installs neither, stays offline and fast.
"""

import itertools
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from split_video import assets, transcribe
from split_video.local_media_store import LocalMediaStore

CLIP = Path(__file__).resolve().parents[1] / "fixtures" / "speech_clip.ogg"
GAP_AT = 5.855  # where the clip's two sentences are joined, by 0.6 s of digital silence
SENTENCE_1 = [
    "mister",
    "quilter",
    "is",
    "the",
    "apostle",
    "of",
    "the",
    "middle",
    "classes",
    "and",
    "we",
    "are",
    "glad",
    "to",
    "welcome",
    "his",
    "gospel",
]
SENTENCE_2 = ["nor", "is", "mister", "quilter's", "manner", "less", "interesting", "than", "his", "matter"]

models_source = os.environ.get("SPLIT_VIDEO_TEST_MODELS")
pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(
        not transcribe.available(), reason="faster-whisper is not installed (uv sync --extra transcribe)"
    ),
    pytest.mark.skipif(
        not models_source or not (Path(models_source) / "models--Systran--faster-whisper-base.en").is_dir(),
        reason="set SPLIT_VIDEO_TEST_MODELS to a folder holding the base.en model",
    ),
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))  # not whatever an earlier import left behind
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    root = tmp_path / "ws"
    root.mkdir()
    shutil.copytree(models_source, root / ".split-video-models")
    shutil.copy(CLIP, root / "clip.ogg")
    return root


def _norm(word):
    return re.sub(r"[^a-z']", "", word.lower())


def _words(doc):
    return [w for s in doc["segments"] for w in s["words"]]


def test_a_short_clip_transcribes_with_word_times_that_line_up(workspace, monkeypatch):
    media = LocalMediaStore(workspace)
    asset = assets.register_path(media, "clip.ogg")

    def no_network(*args, **kwargs):
        raise AssertionError("the model is already in the workspace: no network access is needed")

    monkeypatch.setattr(socket.socket, "connect", no_network)  # offline use
    result = transcribe.transcribe(media, asset.id)
    assert result["made"] and result["model"] == "base.en" and result["language"] == "en"

    doc = transcribe.load(media, asset.id)
    words = _words(doc)
    heard = [_norm(w["word"]) for w in words]
    expected = SENTENCE_1 + SENTENCE_2
    hits = sum(1 for w in expected if w in heard)
    assert hits >= len(expected) - 3, heard  # a small model may fumble a word, not the sentence
    assert "gospel" in heard and "matter" in heard and "apostle" in heard

    # Word times: in order, inside the clip, and the known pause between the sentences is where it should be.
    assert all(0 <= w["start"] <= w["end"] <= doc["duration"] + 0.05 for w in words)
    assert all(a["start"] <= b["start"] for a, b in itertools.pairwise(words))
    gospel = words[heard.index("gospel")]
    nor = words[heard.index("nor")]
    assert gospel["end"] <= GAP_AT + 0.3 and gospel["start"] > 4.0
    assert nor["start"] >= GAP_AT - 0.1 and nor["start"] < GAP_AT + 1.2
    assert words[heard.index("matter")]["end"] > doc["duration"] - 1.0  # the last word is at the end
    assert doc["duration"] == pytest.approx(11.28, abs=0.1)
    assert transcribe.text_between(doc, 0, GAP_AT).split()[-1].lower().startswith("gospel")


def test_a_long_file_is_cut_into_windows_and_stitched_without_repeats(workspace, monkeypatch):
    # Seven copies of the clip, 3 s apart: 100 s of audio, transcribed in windows of about 30 s.
    clip_gap = 3.0
    inputs, parts = [], []
    for i in range(7):
        inputs += ["-i", str(CLIP)]
        parts.append(f"[{i}:a]apad=pad_dur={clip_gap}[p{i}]")
    graph = ";".join(parts) + ";" + "".join(f"[p{i}]" for i in range(7)) + "concat=n=7:v=0:a=1[o]"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[o]", str(workspace / "long.wav")],
        check=True,
    )
    monkeypatch.setattr(transcribe, "WINDOW_SECONDS", 30.0)
    monkeypatch.setattr(transcribe, "SEARCH_SECONDS", 8.0)
    media = LocalMediaStore(workspace)
    asset = assets.register_path(media, "long.wav")
    progress = []
    transcribe.transcribe(media, asset.id, on_progress=lambda d, t: progress.append(d))
    doc = transcribe.load(media, asset.id)
    heard = [_norm(w["word"]) for w in _words(doc)]
    assert heard.count("apostle") == 7 and heard.count("gospel") == 7 and heard.count("matter") == 7  # once each
    period = asset.duration / 7
    starts = [w["start"] for w in _words(doc) if _norm(w["word"]) == "apostle"]
    for k, start in enumerate(starts):  # absolute times: the k-th copy's "apostle" is ~1.5 s into it
        assert k * period + 0.8 < start < k * period + 2.5
    assert progress == sorted(progress) and progress[-1] == pytest.approx(asset.duration, abs=0.5)


@pytest.mark.skipif(not os.environ.get("SPLIT_VIDEO_TEST_DOWNLOAD"), reason="downloads tiny.en (75 MB); opt in")
def test_first_use_downloads_into_the_workspace_and_nothing_under_home(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    shutil.copy(CLIP, root / "clip.ogg")
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache")}
    for var in ("HF_HOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE"):
        env.pop(var, None)
    cmd = [
        sys.executable,
        "-c",
        "from split_video.cli import app; app()",
        "transcribe",
        "clip.ogg",
        "--root",
        str(root),
        "--model",
        "tiny.en",
    ]
    done = subprocess.run([*cmd, "--json"], env=env, capture_output=True, text=True, timeout=600, check=False)
    assert done.returncode == 0, done.stderr
    assert (root / ".split-video-models" / "models--Systran--faster-whisper-tiny.en").is_dir()
    # No Hugging Face cache or model data under the home directory. (onnxruntime, which the VAD uses, writes a
    # 36-byte device id under ~/.cache/Microsoft; that is not ours and holds nothing worth keeping.)
    leaked = [p for p in home.rglob("*") if p.is_file() and "Microsoft" not in p.parts]
    assert not leaked, leaked
