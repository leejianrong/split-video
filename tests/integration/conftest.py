import subprocess

import pytest


def _make_clip(path, specs):
    """Build a small video+audio clip by concatenating lavfi tone/silence segments.

    `specs` is a list of (kind, duration) where kind is "tone" or "silence".
    """
    inputs = []
    filter_parts = []
    for i, (kind, duration) in enumerate(specs):
        if kind == "tone":
            inputs += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
        else:
            inputs += ["-f", "lavfi", "-i", f"anullsrc=duration={duration}"]
        filter_parts.append(f"[{i}:a]")
    inputs += ["-f", "lavfi", "-i", f"color=c=black:size=320x240:duration={sum(d for _, d in specs)}"]
    video_index = len(specs)

    concat_filter = "".join(filter_parts) + f"concat=n={len(specs)}:v=0:a=1[aout]"

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", concat_filter,
        "-map", f"{video_index}:v",
        "-map", "[aout]",
        "-shortest",
        "-c:v", "libx264", "-g", "25", "-keyint_min", "25", "-c:a", "aac",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def three_songs_clip(tmp_path):
    path = tmp_path / "three_songs.mp4"
    _make_clip(
        path,
        [
            ("tone", 5), ("silence", 3),
            ("tone", 5), ("silence", 3),
            ("tone", 5),
        ],
    )
    return path


@pytest.fixture
def quiet_bridge_clip(tmp_path):
    # One song with a brief quiet bridge in the middle that dips below
    # threshold but is too short to count as a real gap between songs.
    path = tmp_path / "quiet_bridge.mp4"
    _make_clip(
        path,
        [
            ("tone", 5), ("silence", 0.5), ("tone", 5),
        ],
    )
    return path


@pytest.fixture
def leading_trailing_silence_clip(tmp_path):
    path = tmp_path / "leading_trailing.mp4"
    _make_clip(
        path,
        [
            ("silence", 2), ("tone", 5), ("silence", 3), ("tone", 5), ("silence", 2),
        ],
    )
    return path
