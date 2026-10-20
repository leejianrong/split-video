"""One real end to end check of the highlight suggestions (SPL-48) on a 4 minute "coding stream" with known content:
five scenes (an editor, a title slide, more code, a terminal with a red FAILED line from 130 s, a clean terminal),
six LibriSpeech clips (tests/fixtures/speech_clip.ogg) at 5, 30, 60, 100, 140 and 170 s, pink noise at 75-90 s and a
chord at 200-235 s, digital silence everywhere else. Every signal is made by its real engine (YAMNet, ffmpeg scene
detection, tesseract, faster-whisper, silencedetect, the loudness pass); then the stream preset must surface the FAILED
screen and every speech clip. Skipped without any of them (see test_time_index_real.py)."""

import itertools
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from screen_clip import mono_font

from split_video import assets, highlights, ocr, project, scenes, transcribe
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.local_media_store import LocalMediaStore

CLIP = Path(__file__).resolve().parents[1] / "fixtures" / "speech_clip.ogg"
SPEECH_AT = [5, 30, 60, 100, 140, 170]
SPEECH_SECONDS = 11.28
FAILED_LINE = "FAILED tests/unit/test_scenes.py::test_cut_burst_keeps_strongest"
FAILED_AT = (130.0, 160.0)
NOISE, CHORD = (75, 90), (200, 235)

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


def _esc(text):
    return text.replace(":", "\\:").replace("'", "\\'")


def _run(cmd, cwd):
    done = subprocess.run(cmd, capture_output=True, check=False, cwd=cwd)
    assert done.returncode == 0, done.stderr.decode()[-600:]


def _make_recording(folder: Path) -> None:
    font = mono_font()
    scenes_spec = [  # (seconds, background, lines, red line, big title, text colour)
        (
            45,
            "0x1e1e1e",
            ["import os, sys", "def cut_burst(frames, limit):", "    best = max(frames, key=score)"],
            None,
            None,
            "0xd4d4d4",
        ),
        (45, "0xf2f2f2", [], None, "Quarterly Review", "black"),
        (
            40,
            "0x1e1e1e",
            ["def render_timeline(project):", "    for clip in project.clips:", "        probe(clip.asset)"],
            None,
            None,
            "0xd4d4d4",
        ),
        (30, "0x1e1e1e", ["$ uv run pytest tests/unit -x", "collected 412 items"], FAILED_LINE, None, "0xd4d4d4"),
        (
            80,
            "0xe8e8e8",
            ["$ git status", "On branch main", "nothing to commit, working tree clean"],
            None,
            None,
            "0x202020",
        ),
    ]
    names = []
    for k, (seconds, bg, lines, red, big, fg) in enumerate(scenes_spec):
        draws = [
            f"drawtext=fontfile={font}:text='{_esc(line)}':fontcolor={fg}:fontsize=30:x=60:y={60 + 48 * i}"
            for i, line in enumerate(lines)
        ]
        if red:
            draws.append(f"drawtext=fontfile={font}:text='{_esc(red)}':fontcolor=0xff3030:fontsize=28:x=60:y=300")
        if big:
            draws.append(
                f"drawtext=fontfile={font}:text='{_esc(big)}':fontcolor=black:fontsize=96:x=(w-text_w)/2:y=(h-text_h)/2"
            )
        name = f"seg_{k}.mp4"
        names.append(name)
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={bg}:s=1280x720:r=10:d={seconds}"]
        cmd += [
            "-vf",
            ",".join(draws) or "null",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "20",
            name,
        ]
        _run(cmd, folder)
    (folder / "list.txt").write_text("".join(f"file {n}\n" for n in names))
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-i", "list.txt", "-c", "copy", "video_only.mp4"], folder)
    inputs, graph, labels = ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=240"], [], ["[0]"]
    for t in SPEECH_AT:
        n = len(labels)
        inputs += ["-i", str(CLIP)]
        graph.append(f"[{n}]aresample=16000,pan=mono|c0=c0,adelay={t * 1000}[s{n}]")
        labels.append(f"[s{n}]")
    n = len(labels)
    inputs += ["-f", "lavfi", "-i", f"anoisesrc=d={NOISE[1] - NOISE[0]}:c=pink:r=16000:a=0.5"]
    graph.append(f"[{n}]tremolo=f=6:d=0.6,adelay={NOISE[0] * 1000}[nz]")
    chord = "0.25*sin(2*PI*261.6*t)+0.2*sin(2*PI*329.6*t)+0.2*sin(2*PI*392*t)"
    inputs += ["-f", "lavfi", "-i", f"aevalsrc='{chord}':d={CHORD[1] - CHORD[0]}:s=16000"]
    graph.append(f"[{n + 1}]adelay={CHORD[0] * 1000}[mu]")
    graph.append(
        "".join(labels) + f"[nz][mu]amix=inputs={len(labels) + 2}:normalize=0:duration=first,aresample=44100[a]"
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            *inputs,
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[a]",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "audio.m4a",
        ],
        folder,
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            "video_only.mp4",
            "-i",
            "audio.m4a",
            "-c",
            "copy",
            "-t",
            "240",
            "ws/recording.mp4",
        ],
        folder,
    )


def _overlap(c, a, b):
    return min(c["end"], b) - max(c["start"], a)


def test_failed_screen_and_speech_clips_surface_as_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    (tmp_path / "ws").mkdir()
    _make_recording(tmp_path)
    root = tmp_path / "ws"
    shutil.copytree(models_source, root / ".split-video-models")
    media = LocalMediaStore(root)
    asset = assets.register_path(media, "recording.mp4")
    assert asset.duration == pytest.approx(240.0, abs=0.5)

    with pytest.raises(highlights.NoSignalsError, match="transcribe"):  # nothing made yet
        highlights.suggest_highlights(media, asset.id)

    cache = ClassificationCache(media, asset.path)
    project.run_analysis(media, asset.path, cache, cache.get_thresholds())
    SilenceCache(media, asset.path).get_raw_silences(-35.0)
    WaveformCache(media, asset.path).get_loudness()
    assert transcribe.transcribe(media, asset.id)["made"]
    assert scenes.analyze(media, asset.id)["scenes"] >= 1
    assert ocr.recognize(media, asset.id, interval=10)["ocr_frames"] >= 1

    found = highlights.suggest_highlights(
        media, asset.id, preset="stream", count=10, min_seconds=8, max_seconds=30, target_seconds=12
    )
    assert found["signals"]["used"] == list(highlights.POSITIVE) and found["signals"]["missing"] == []
    print(
        "\n"
        + "\n".join(
            f"{c['rank']:>2} {c['start']:>7} {c['end']:>7} {c['score']:.2f} {c['reasons'][0]['note']}"
            for c in found["candidates"]
        )
    )

    top = found["candidates"][0]  # the FAILED screen, with the clip that is spoken over it
    assert _overlap(top, *FAILED_AT) >= 8 and "FAILED" in top["title_hint"]
    assert any(r["signal"] == "keywords" and "FAILED" in r["note"] for r in top["reasons"])
    for t in SPEECH_AT:  # every speech clip is (most of) a candidate
        hit = max(found["candidates"], key=lambda c: _overlap(c, t, t + SPEECH_SECONDS))
        assert _overlap(hit, t, t + SPEECH_SECONDS) >= 8, f"the clip at {t} s is not a candidate"
        assert any(r["signal"] == "speech" for r in hit["reasons"])
    assert any(_overlap(c, *CHORD) >= 20 for c in found["candidates"])  # the chord: loud sound that is not speech
    starts = sorted((c["start"], c["end"]) for c in found["candidates"])
    assert all(b[0] >= a[1] + 10 - 1e-6 for a, b in itertools.pairwise(starts))  # no overlap, 10 s apart

    # the same recording through the other presets: the chord leads a gig, still no error
    gig = highlights.suggest_highlights(media, asset.id, preset="gig", count=3, min_seconds=8, max_seconds=40)
    assert _overlap(gig["candidates"][0], *CHORD) >= 20
