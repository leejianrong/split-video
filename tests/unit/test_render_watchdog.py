"""`_run_ffmpeg` against fake ffmpeg programs: a hung one is killed, a chatty one cannot deadlock it."""

import stat
import threading
import time

import pytest

from split_video import render
from split_video.ffmpeg import ExtractError, stall_seconds
from split_video.jobs import JobCancelledError
from split_video.render import Plan, _run_ffmpeg

PLAN = Plan(clips=[], graph="nullsrc[v];anullsrc[a]", video_label="v", audio_label="a", width=16, height=16)


class Token:
    """A minimal CancelToken."""

    def __init__(self):
        self._cancelled = threading.Event()
        self._callbacks = []

    def cancel(self):
        self._cancelled.set()
        for cb in list(self._callbacks):
            cb()

    def is_cancelled(self):
        return self._cancelled.is_set()

    def raise_if_cancelled(self):
        if self._cancelled.is_set():
            raise JobCancelledError()

    def on_cancel(self, callback):
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback) if callback in self._callbacks else None


def _fake_ffmpeg(tmp_path, monkeypatch, body):
    script = tmp_path / "ffmpeg"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(render, "_require_binary", lambda name: str(script))


def test_a_hung_ffmpeg_is_killed_with_a_clear_error(tmp_path, monkeypatch):
    # Prints one progress block, then sits there forever without advancing.
    _fake_ffmpeg(tmp_path, monkeypatch, "echo out_time_us=1000\necho 'about to hang' >&2\nexec sleep 60\n")
    started = time.monotonic()
    with pytest.raises(ExtractError, match="stopped making progress.*killed.*about to hang"):
        _run_ffmpeg(PLAN, [], str(tmp_path / "out.mp4"), 10.0, None, stall_timeout=1.0)
    assert time.monotonic() - started < 15


def test_repeated_progress_that_does_not_advance_still_counts_as_hung(tmp_path, monkeypatch):
    _fake_ffmpeg(tmp_path, monkeypatch, "while true; do echo out_time_us=5000; sleep 0.1; done\n")
    with pytest.raises(ExtractError, match="stopped making progress"):
        _run_ffmpeg(PLAN, [], str(tmp_path / "out.mp4"), 10.0, None, stall_timeout=1.0)


def test_advancing_progress_is_not_killed_and_reports(tmp_path, monkeypatch):
    _fake_ffmpeg(tmp_path, monkeypatch, "for i in 1 2 3 4 5 6; do echo out_time_us=${i}00000; sleep 0.3; done\n")
    seen = []
    _run_ffmpeg(PLAN, [], str(tmp_path / "out.mp4"), 10.0, seen.append, stall_timeout=1.0)
    assert seen[-1] == 1.0 and len(seen) >= 6


def test_a_flood_of_stderr_does_not_deadlock_the_progress_loop(tmp_path, monkeypatch):
    # 2 MB of log before the first progress line: far past any pipe buffer.
    _fake_ffmpeg(
        tmp_path, monkeypatch, "head -c 2000000 /dev/zero | tr '\\0' x >&2\necho out_time_us=1000000\nexit 0\n"
    )
    _run_ffmpeg(PLAN, [], str(tmp_path / "out.mp4"), 10.0, None, stall_timeout=20.0)


def test_cancel_still_kills_a_hung_ffmpeg(tmp_path, monkeypatch):
    _fake_ffmpeg(tmp_path, monkeypatch, "exec sleep 60\n")
    cancel = Token()
    threading.Timer(0.5, cancel.cancel).start()
    with pytest.raises(JobCancelledError):
        _run_ffmpeg(PLAN, [], str(tmp_path / "out.mp4"), 10.0, None, cancel, stall_timeout=30.0)


def test_the_stall_limit_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("SPLIT_VIDEO_FFMPEG_STALL_SECONDS", raising=False)
    assert stall_seconds() == 300.0
    monkeypatch.setenv("SPLIT_VIDEO_FFMPEG_STALL_SECONDS", "42")
    assert stall_seconds() == 42.0
    monkeypatch.setenv("SPLIT_VIDEO_FFMPEG_STALL_SECONDS", "0")
    assert stall_seconds() is None
    monkeypatch.setenv("SPLIT_VIDEO_FFMPEG_STALL_SECONDS", "junk")
    assert stall_seconds() == 300.0
