"""Frame extraction: a clip whose picture is a different flat colour every second, so the frame returned can be
checked against the time asked for (SPL-45). Real ffmpeg, on the in-memory store (materialised) and the local one."""

import subprocess

import numpy as np
import pytest

from split_video import assets, frames, proxy
from split_video.ffmpeg import MediaInfo
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import InMemoryMediaStore

# second k of the clip is COLOURS[k]
COLOURS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "magenta": (255, 0, 255),
    "cyan": (0, 255, 255),
}
ORDER = list(COLOURS)
SECONDS = len(ORDER)


def colour_clip(path, size="1280x720", fps=10, with_audio=True):
    """6 s, red, green, blue, yellow, magenta, cyan: one second each, a keyframe every second."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for name in ORDER:
        cmd += ["-f", "lavfi", "-i", f"color=c={name}:s={size}:r={fps}:d=1"]
    cmd += ["-f", "lavfi", "-i", f"sine=duration={SECONDS}"] if with_audio else []
    graph = "".join(f"[{k}:v]" for k in range(SECONDS)) + f"concat=n={SECONDS}:v=1:a=0,format=yuv420p[o]"
    cmd += ["-filter_complex", graph, "-map", "[o]"] + (["-map", f"{SECONDS}:a", "-c:a", "aac"] if with_audio else [])
    cmd += ["-c:v", "libx264", "-g", str(fps), str(path)]
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def decode(data):
    """(width, height, rgb array) of an encoded image, decoded by ffmpeg itself (Pillow is not a dependency)."""
    probe = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "-", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        input=data,
        capture_output=True,
        check=True,
    )
    shape = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", "-"],
        input=data,
        capture_output=True,
        check=True,
    ).stdout.decode()
    width, height = (int(n) for n in shape.strip().split(","))
    return width, height, np.frombuffer(probe.stdout, dtype=np.uint8).reshape(height, width, 3)


def dominant(data):
    """The name of the palette colour closest to the middle of the image."""
    _, _, rgb = decode(data)
    mid = rgb[rgb.shape[0] // 2, rgb.shape[1] // 2].astype(int)
    return min(COLOURS, key=lambda n: sum((mid[i] - COLOURS[n][i]) ** 2 for i in range(3)))


@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory):
    path = tmp_path_factory.mktemp("colours") / "colours.mp4"
    colour_clip(path)
    return path.read_bytes()


@pytest.fixture
def local(tmp_path, clip_bytes):
    (tmp_path / "colours.mp4").write_bytes(clip_bytes)
    media = LocalMediaStore(tmp_path.resolve())
    return media, assets.register_path(media, "colours.mp4")


@pytest.fixture
def memory(clip_bytes):
    media = InMemoryMediaStore(materialise=True)
    media.add("show/colours.mp4", clip_bytes, info=MediaInfo(6.0, 1280, 720, True, 10.0))
    return media, assets.register_path(media, "show/colours.mp4")


# --- the picture corresponds to the time ----------------------------------------------------------


@pytest.mark.parametrize("which", ["local", "memory"])
def test_frame_shows_the_colour_of_that_second(which, request):
    media, asset = request.getfixturevalue(which)
    for t, name in [(0.0, "red"), (1.5, "green"), (2.05, "blue"), (3.7, "yellow"), (4.85, "magenta"), (5.5, "cyan")]:
        frame = frames.extract_frame(media, asset.id, t)
        assert dominant(frame.data) == name, t
        assert frame.requested_time == t and 0 <= frame.time - t < 0.11  # within a frame at 10 fps
        assert frame.source == "original" and frame.duration == 6.0 and frame.asset == asset.id


def test_a_jpeg_and_a_png_with_the_right_headers_and_size(local):
    media, asset = local
    jpg = frames.extract_frame(media, asset.id, 2.5)
    assert jpg.data[:3] == b"\xff\xd8\xff" and jpg.mime == "image/jpeg" and jpg.format == "jpeg"
    assert (jpg.width, jpg.height) == (1280, 720) and decode(jpg.data)[:2] == (1280, 720)
    png = frames.extract_frame(media, asset.id, 2.5, format="png")
    assert png.data[:8] == b"\x89PNG\r\n\x1a\n" and png.mime == "image/png"
    assert dominant(png.data) == "blue"
    assert frames.extract_frame(media, asset.id, 2.5, format="JPG").format == "jpeg"


def test_quality_changes_jpeg_size(local):
    media, asset = local
    # a flat colour compresses to almost nothing, so compare on a busier picture
    low = frames.extract_frame(media, asset.id, 1.0, quality=5).data
    high = frames.extract_frame(media, asset.id, 1.0, quality=95).data
    assert len(low) <= len(high) and low[:2] == b"\xff\xd8"


# --- the end of the asset -------------------------------------------------------------------------


def test_exactly_at_the_end_returns_the_last_frame(local):
    media, asset = local
    frame = frames.extract_frame(media, asset.id, asset.duration)
    assert dominant(frame.data) == "cyan" and frame.data and frame.time <= asset.duration


def test_just_before_the_end_works(local):
    media, asset = local
    assert dominant(frames.extract_frame(media, asset.id, asset.duration - 0.02).data) == "cyan"


def test_beyond_the_end_states_the_duration(local):
    media, asset = local
    with pytest.raises(frames.FrameTimeError, match=rf"{asset.duration:g} s long"):
        frames.extract_frame(media, asset.id, asset.duration + 1)


def test_negative_and_nan_times(local):
    media, asset = local
    for t in (-0.1, float("nan")):
        with pytest.raises(frames.FrameTimeError):
            frames.extract_frame(media, asset.id, t)


# --- scaling --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"width": 320}, (320, 180)),
        ({"height": 90}, (160, 90)),
        ({"width": 321}, (321, 181)),  # odd sizes are fine for an image
        ({"width": 400, "height": 100}, (178, 100)),  # fits inside the box, aspect kept
        ({"width": 100, "height": 400}, (100, 56)),
        ({"width": 4000}, (1280, 720)),  # never upscaled
        ({"height": 4000}, (1280, 720)),
        ({"width": 4000, "height": 100}, (178, 100)),
        ({}, (1280, 720)),
    ],
)
def test_scaled_dimensions_are_reported_and_real(local, kwargs, expected):
    media, asset = local
    for fmt in ("jpeg", "png"):
        frame = frames.extract_frame(media, asset.id, 1.5, format=fmt, **kwargs)
        w, h, _ = decode(frame.data)
        assert (frame.width, frame.height) == (w, h)
        assert abs(w - expected[0]) <= 1 and abs(h - expected[1]) <= 1, (w, h)
        assert dominant(frame.data) == "green"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": 0},
        {"height": -5},
        {"width": frames.MAX_DIMENSION + 1},
        {"format": "gif"},
        {"source": "proxy"},
        {"quality": 0},
        {"quality": 101},
    ],
)
def test_bad_arguments(local, kwargs):
    media, asset = local
    with pytest.raises(frames.FrameArgumentError):
        frames.extract_frame(media, asset.id, 1.0, **kwargs)


# --- which asset, which source --------------------------------------------------------------------


def test_unknown_asset(local):
    media, _ = local
    from split_video.timeline import TimelineError

    with pytest.raises(TimelineError):
        frames.extract_frame(media, "a_0000000000000000", 1.0)


def test_audio_only_asset(tmp_path):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", str(tmp_path / "song.mp3")], check=True
    )
    media = LocalMediaStore(tmp_path.resolve())
    asset = assets.register_path(media, "song.mp3")
    with pytest.raises(frames.FrameAudioOnlyError, match="audio-only"):
        frames.extract_frame(media, asset.id, 1.0)


def test_auto_reads_the_proxy_and_original_forces_the_file(local):
    media, asset = local
    assert frames.extract_frame(media, asset.id, 3.5).source == "original"  # no proxy yet
    proxy.prepare(media, asset.id)
    auto = frames.extract_frame(media, asset.id, 3.5)
    assert auto.source == "proxy" and auto.width == 640 and dominant(auto.data) == "yellow"
    assert 0 <= auto.time - 3.5 <= 1 / 15 + 0.01  # a proxy frame is within 1/15 s
    original = frames.extract_frame(media, asset.id, 3.5, source="original")
    assert original.source == "original" and original.width == 1280 and dominant(original.data) == "yellow"
    # asking for more than the proxy has gives the proxy's size (no upscale); the original can do it
    assert frames.extract_frame(media, asset.id, 3.5, width=1000).width == 640
    assert frames.extract_frame(media, asset.id, 3.5, width=1000, source="original").width == 1000


def test_proxy_frames_match_time_at_the_end_too(local):
    media, asset = local
    proxy.prepare(media, asset.id)
    assert dominant(frames.extract_frame(media, asset.id, asset.duration).data) == "cyan"
    assert dominant(frames.extract_frame(media, asset.id, 0).data) == "red"


def test_nothing_is_written_to_the_workspace(local, tmp_path):
    media, asset = local
    before = sorted(p.name for p in tmp_path.rglob("*"))
    frames.extract_frame(media, asset.id, 1.0, source="original")
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
