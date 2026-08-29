"""Subprocess boundary: everything that shells out to ffmpeg/ffprobe."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from split_video.silence import SilenceInterval, parse_silencedetect_output


class FfmpegNotFoundError(RuntimeError):
    pass


class ProbeError(RuntimeError):
    pass


class ExtractError(RuntimeError):
    pass


WAVEFORM_SAMPLE_RATE = 8000


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise FfmpegNotFoundError(f"'{name}' not found on PATH; please install ffmpeg.")
    return path


def probe_duration(path: Path) -> float:
    ffprobe = _require_binary("ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ProbeError(f"could not read '{path}': {result.stderr.strip()}")
    return float(result.stdout.strip())


def detect_silence(path: Path, threshold: str, min_silence_duration: float) -> list[SilenceInterval]:
    ffmpeg = _require_binary("ffmpeg")
    total_duration = probe_duration(path)
    result = subprocess.run(
        [
            ffmpeg,
            "-i", str(path),
            "-af", f"silencedetect=noise={threshold}:d={min_silence_duration}",
            "-f", "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return parse_silencedetect_output(result.stderr, total_duration)


def extract_pcm_audio(path: Path, sample_rate: int = WAVEFORM_SAMPLE_RATE) -> bytes:
    """Decode a file's audio track to raw mono 16-bit PCM, for waveform-peak computation.

    A low sample rate keeps the piped output small: only a crude amplitude
    envelope is needed here, not audio fidelity.
    """
    ffmpeg = _require_binary("ffmpeg")
    result = subprocess.run(
        [
            ffmpeg,
            "-i", str(path),
            "-vn",
            "-ac", "1",
            "-ar", str(sample_rate),
            "-f", "s16le",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExtractError(f"ffmpeg failed to decode audio from '{path.name}': {result.stderr.decode(errors='replace').strip()[-2000:]}")
    return result.stdout


def extract_segment(
    path: Path,
    start: float,
    end: float,
    out_path: Path,
    precise: bool,
    output_format: str | None = None,
) -> None:
    ffmpeg = _require_binary("ffmpeg")
    duration = end - start

    if precise or output_format is not None:
        # Output-side seeking: slower (decodes from the start) but frame-accurate.
        args = [ffmpeg, "-y", "-i", str(path), "-ss", f"{start:.3f}", "-t", f"{duration:.3f}"]
    else:
        # Input-side seeking: fast, snaps to the nearest keyframe.
        args = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{duration:.3f}", "-c", "copy"]

    args.append(str(out_path))
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ExtractError(f"ffmpeg failed for '{out_path.name}': {result.stderr.strip()[-2000:]}")
