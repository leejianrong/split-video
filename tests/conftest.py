import pytest


@pytest.fixture(autouse=True)
def _no_ambient_api_token(monkeypatch):
    """A SPLIT_VIDEO_TOKEN in the developer's shell must not switch the suite out of local mode."""
    monkeypatch.delenv("SPLIT_VIDEO_TOKEN", raising=False)
