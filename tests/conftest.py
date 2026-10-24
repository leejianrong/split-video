import subprocess

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_api_token(monkeypatch):
    """A SPLIT_VIDEO_TOKEN in the developer's shell must not switch the suite out of local mode."""
    monkeypatch.delenv("SPLIT_VIDEO_TOKEN", raising=False)


# The tests build their fixture media with plain `subprocess.run(["ffmpeg", ...])` in dozens of places. Rather than
# edit each one, every such call gets a timeout and a closed stdin unless the test says otherwise, so a stuck ffmpeg
# fails that test in two minutes instead of blocking forever.
SUBPROCESS_TIMEOUT_SECONDS = 120


@pytest.fixture(autouse=True)
def _bounded_subprocess_run(monkeypatch):
    real_run = subprocess.run

    def run(*args, **kwargs):
        kwargs.setdefault("timeout", SUBPROCESS_TIMEOUT_SECONDS)
        if "input" not in kwargs and "stdin" not in kwargs:
            kwargs["stdin"] = subprocess.DEVNULL
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
