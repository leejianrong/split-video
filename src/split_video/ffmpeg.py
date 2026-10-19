"""Subprocess boundary: everything that shells out to ffmpeg/ffprobe."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from split_video.jobs import CancelToken
from split_video.silence import SilenceInterval, SilenceParser


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


# How long a terminated ffmpeg gets to exit before it is killed.
KILL_GRACE_SECONDS = 2.0


def stop_process(proc: subprocess.Popen[Any], grace: float | None = None) -> None:
    """Terminate `proc`, and kill it if it is still running after `grace` seconds (default
    `KILL_GRACE_SECONDS`). Returns at once."""
    grace = KILL_GRACE_SECONDS if grace is None else grace
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return

    def reap() -> None:
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            proc.kill()

    threading.Thread(target=reap, name="ffmpeg-reaper", daemon=True).start()


def stop_on_cancel(proc: subprocess.Popen[Any], cancel: CancelToken | None) -> Callable[[], None]:
    """Stop `proc` as soon as `cancel` fires. Returns a function that detaches again (call it once
    the process is done); with no token it does nothing."""
    if cancel is None:
        return lambda: None
    return cancel.on_cancel(lambda: stop_process(proc))


def run_cancellable(
    cmd: list[str], cancel: CancelToken | None = None, *, text: bool = True
) -> subprocess.CompletedProcess[Any]:
    """`subprocess.run(cmd, capture_output=True)` that a cancelled job can interrupt: the process
    is terminated at once and `JobCancelledError` is raised in place of a result. Without a token
    this is a plain `subprocess.run`."""
    if cancel is None:
        return subprocess.run(cmd, capture_output=True, text=text, check=False)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text)
    detach = stop_on_cancel(proc, cancel)
    try:
        stdout, stderr = proc.communicate()
    finally:
        detach()
    cancel.raise_if_cancelled()
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def run_with_progress(
    cmd: list[str], on_seconds: Callable[[float], None], cancel: CancelToken | None = None
) -> subprocess.CompletedProcess[str]:
    """Run an ffmpeg command, calling `on_seconds(out_time)` as it writes output (parsed from
    `-progress pipe:1`, so it is output time, not wall time). A cancelled job terminates it at
    once and gets `JobCancelledError`. `cmd[0]` must be the ffmpeg binary. stdout carries the
    progress, so the output must be a file; stderr is returned (its last 8 KiB)."""
    full = [cmd[0], "-nostdin", "-nostats", "-progress", "pipe:1", *cmd[1:]]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    tail: list[str] = []

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line)
            del tail[:-200]

    reader = threading.Thread(target=drain, name="ffmpeg-stderr", daemon=True)
    reader.start()
    detach = stop_on_cancel(proc, cancel)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.lstrip("-").isdigit():
                on_seconds(max(0, int(value)) / 1e6)
        proc.wait()
        reader.join(5)
    finally:
        detach()
    if cancel is not None:
        cancel.raise_if_cancelled()
    return subprocess.CompletedProcess(full, proc.returncode, "", "".join(tail)[-8192:])


def probe_duration(path: Path) -> float:
    ffprobe = _require_binary("ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ProbeError(f"could not read '{path}': {result.stderr.strip()}")
    return float(result.stdout.strip())


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int | None  # None when the file has no video stream
    height: int | None
    has_audio: bool
    fps: float | None = None  # the first video stream's average frame rate; None without video or when unknown


def probe_media(path: Path) -> MediaInfo:
    """Duration, video size and whether there's an audio stream, in one ffprobe call."""
    ffprobe = _require_binary("ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ProbeError(f"could not read '{path}': {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError):
        raise ProbeError(f"could not read a duration from '{path}'") from None
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("width")), None)
    return MediaInfo(
        duration=duration,
        width=video["width"] if video else None,
        height=video["height"] if video else None,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        fps=_parse_rate(video.get("avg_frame_rate")) if video else None,
    )


def _parse_rate(text: object) -> float | None:
    """ffprobe's "30000/1001" (or "25") as a float; None for "0/0" and other junk."""
    try:
        num, _, den = str(text).partition("/")
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return round(value, 3) if value > 0 else None


def detect_silence(
    path: Path, threshold: str, min_silence_duration: float, cancel: CancelToken | None = None
) -> list[SilenceInterval]:
    """Run ffmpeg's silencedetect over the audio. The log is read line by line as ffmpeg writes it and
    only the parsed intervals are kept, so memory does not grow with the length of the recording. The
    video is not decoded (`-vn`). A cancelled job terminates ffmpeg and gets `JobCancelledError`."""
    ffmpeg = _require_binary("ffmpeg")
    cmd = [
        ffmpeg,
        "-nostdin",
        "-nostats",
        "-i",
        str(path),
        "-vn",
        "-af",
        f"silencedetect=noise={threshold}:d={min_silence_duration}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
    parser = SilenceParser()
    detach = stop_on_cancel(proc, cancel)
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            parser.feed(line)
        proc.wait()
    finally:
        detach()
        stop_process(proc)
    if cancel is not None:
        cancel.raise_if_cancelled()
    if parser.pending_start is not None:  # the file ended while still silent
        parser.intervals.append(SilenceInterval(start=parser.pending_start, end=probe_duration(path)))
    return parser.intervals


def extract_pcm_audio(path: Path, sample_rate: int = WAVEFORM_SAMPLE_RATE, cancel: CancelToken | None = None) -> bytes:
    """Decode a file's audio track to raw mono 16-bit PCM, for waveform-peak computation.

    A low sample rate keeps the piped output small: only a crude amplitude
    envelope is needed here, not audio fidelity.
    """
    ffmpeg = _require_binary("ffmpeg")
    result = run_cancellable(
        [ffmpeg, "-i", str(path), "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"], cancel, text=False
    )
    if result.returncode != 0:
        raise ExtractError(
            f"ffmpeg failed to decode audio from '{path.name}': {result.stderr.decode(errors='replace').strip()[-2000:]}"
        )
    return result.stdout


# Seconds of audio per chunk yielded by `iter_pcm_chunks` (160 KB at 16 kHz mono s16).
PCM_CHUNK_SECONDS = 5.0


def iter_pcm_chunks(
    path: Path,
    sample_rate: int = WAVEFORM_SAMPLE_RATE,
    chunk_seconds: float = PCM_CHUNK_SECONDS,
    cancel: CancelToken | None = None,
) -> Iterator[np.ndarray]:
    """Decode a file's audio to mono 16-bit PCM and yield it as int16 arrays of `chunk_seconds` each
    (the last one is shorter), straight off ffmpeg's stdout pipe, so memory stays at one chunk however
    long the file is. This is the streaming `extract_pcm_audio`: same ffmpeg arguments, same
    `ExtractError` message when ffmpeg fails.

    stderr is drained on a thread (only its tail is kept), so ffmpeg can never block on a full pipe. A
    cancelled job terminates ffmpeg at once and gets `JobCancelledError` from the generator; closing
    the generator early (or an exception in the consumer) stops ffmpeg too."""
    ffmpeg = _require_binary("ffmpeg")
    cmd = [ffmpeg, "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tail: list[bytes] = []

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line)
            del tail[:-200]

    reader = threading.Thread(target=drain, name="ffmpeg-stderr", daemon=True)
    reader.start()
    detach = stop_on_cancel(proc, cancel)
    chunk_bytes = max(1, int(sample_rate * chunk_seconds)) * 2
    try:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(chunk_bytes)  # blocks until a full chunk or EOF
            if not data:
                break
            yield np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2")
        proc.wait()
        reader.join(5)
    finally:
        detach()
        stop_process(proc)
        if proc.stdout is not None:
            proc.stdout.close()
    if cancel is not None:
        cancel.raise_if_cancelled()
    if proc.returncode != 0:
        message = b"".join(tail).decode(errors="replace").strip()[-2000:]
        raise ExtractError(f"ffmpeg failed to decode audio from '{path.name}': {message}")


def extract_segment(
    path: Path,
    start: float,
    end: float,
    out_path: Path,
    precise: bool,
    output_format: str | None = None,
    cancel: CancelToken | None = None,
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
    result = run_cancellable(args, cancel)
    if result.returncode != 0:
        raise ExtractError(f"ffmpeg failed for '{out_path.name}': {result.stderr.strip()[-2000:]}")
