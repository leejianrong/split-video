"""Proxies and thumbnail strips: the planning maths, currency rules and cleanup, with real ffmpeg on a tiny clip."""

import hashlib
import itertools
import subprocess

import pytest

from split_video import assets, proxy
from split_video.ffmpeg import ExtractError, MediaInfo, probe_media


def _image_size(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return tuple(int(n) for n in out.strip().split(","))


from split_video.jobs import JobCancelledError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import InMemoryMediaStore


def _clip(path, seconds=12, size="640x360", fps=25):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}"]
    cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-g", "250"]
    cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


@pytest.fixture
def local(tmp_path):
    _clip(tmp_path / "gig.mp4")
    media = LocalMediaStore(tmp_path.resolve())
    asset = assets.register_path(media, "gig.mp4")
    return media, asset, tmp_path


class Cancelled:
    """A token that is already cancelled."""

    def is_cancelled(self):
        return True

    def raise_if_cancelled(self):
        raise JobCancelledError("job cancelled")

    def on_cancel(self, callback):
        callback()
        return lambda: None


# --- the maths ------------------------------------------------------------------------------


def test_short_video_one_tile_per_ten_seconds():
    strip = proxy.plan_strip(95.0, 1920, 1080)
    assert (strip.interval, strip.count, strip.columns, strip.rows) == (10.0, 10, 10, 1)
    assert (strip.tile_width, strip.tile_height) == (160, 90)


def test_long_video_is_capped_in_tiles_and_height():
    strip = proxy.plan_strip(3 * 3600, 1920, 1080)
    assert strip.count == proxy.MAX_TILES == 240 and strip.interval == 45.0
    assert (strip.columns, strip.rows) == (10, 24)
    assert strip.columns * strip.rows >= strip.count and strip.tile_height * strip.rows <= 2200


def test_tiny_and_odd_shapes_get_even_tiles():
    assert proxy.plan_strip(0.5, 100, 100).count == 1
    assert proxy.plan_strip(30, 1080, 1920).tile_height == 284  # portrait video: taller tiles


# --- real ffmpeg ------------------------------------------------------------------------------


def test_prepare_makes_both_files_in_the_sidecar_and_is_idempotent(local):
    media, asset, root = local
    before = (hashlib.sha256((root / "gig.mp4").read_bytes()).hexdigest(), (root / "gig.mp4").stat().st_mtime_ns)
    assert proxy.flags(media, asset) == {"proxy": False, "thumbnails": False}
    assert proxy.visual_source(media, asset.id) == "gig.mp4"

    first = proxy.prepare(media, asset.id)
    assert first["made"] == ["proxy", "thumbnails"]
    sidecar = root / ".split-video-cache"
    assert sorted(p.name for p in sidecar.iterdir() if "proxy.mp4" in p.name or "thumbs" in p.name) == [
        "gig.mp4.proxy.mp4",
        "gig.mp4.thumbs.jpg",
    ]
    info = probe_media(sidecar / "gig.mp4.proxy.mp4")
    assert (info.width, info.height) == (640, 360) and info.fps == 15 and info.has_audio
    assert info.duration == pytest.approx(12, abs=0.5)
    meta = first["thumbnails"]
    assert (
        _image_size(sidecar / "gig.mp4.thumbs.jpg") == (meta["width"], meta["height"]) == (320, 90)
    )  # 2 tiles of 160x90 in one row
    assert meta["source"] == "proxy" and meta["count"] == 2

    assert proxy.flags(media, asset) == {"proxy": True, "thumbnails": True}
    assert proxy.visual_source(media, asset.id) == ".split-video-cache/gig.mp4.proxy.mp4"

    second = proxy.prepare(media, asset.id)
    assert second["made"] == [] and second["proxy"] == first["proxy"]
    assert proxy.prepare(media, asset.id, force=True)["made"] == ["proxy", "thumbnails"]

    # The original is never modified.
    assert before == (
        hashlib.sha256((root / "gig.mp4").read_bytes()).hexdigest(),
        (root / "gig.mp4").stat().st_mtime_ns,
    )


def test_proxy_keyframes_are_two_seconds_apart_and_the_index_is_up_front(local):
    media, asset, root = local
    proxy.prepare(media, asset.id)
    path = root / ".split-video-cache" / "gig.mp4.proxy.mp4"
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0"]
        + [str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    keys = [float(line.split(",")[0]) for line in out.splitlines() if "K" in line.split(",")[1]]
    assert keys[:4] == [0.0, 2.0, 4.0, 6.0]
    head = path.read_bytes()[:4096]
    assert 0 <= head.find(b"moov") < head.find(b"mdat") or head.find(b"mdat") == -1  # faststart


def test_a_small_video_is_not_upscaled_and_slow_fps_is_kept(tmp_path):
    _clip(tmp_path / "small.mp4", seconds=4, size="200x100", fps=10)
    media = LocalMediaStore(tmp_path.resolve())
    asset = assets.register_path(media, "small.mp4")
    meta = proxy.prepare(media, asset.id)["proxy"]
    info = probe_media(tmp_path / ".split-video-cache" / "small.mp4.proxy.mp4")
    assert (info.width, info.height, info.fps) == (200, 100, 10) and meta["width"] == 200


def test_a_changed_original_makes_the_proxy_stale(local):
    media, asset, root = local
    proxy.prepare(media, asset.id)
    (root / "gig.mp4").touch()  # a new mtime, same bytes
    stale = assets.get_asset(media, asset.id)
    assert proxy.flags(media, stale) == {"proxy": False, "thumbnails": False}
    assert proxy.visual_source(media, asset.id) == "gig.mp4"
    assert proxy.prepare(media, asset.id)["made"] == ["proxy", "thumbnails"]


def test_a_truncated_proxy_file_is_not_current(local):
    media, asset, root = local
    proxy.prepare(media, asset.id)
    path = root / ".split-video-cache" / "gig.mp4.proxy.mp4"
    path.write_bytes(path.read_bytes()[:1000])
    assert proxy.proxy_info(media, asset) is None


def test_a_cancelled_run_leaves_no_partial_files(local):
    media, asset, root = local
    with pytest.raises(JobCancelledError):
        proxy.prepare(media, asset.id, cancel=Cancelled())
    leftovers = (
        [p.name for p in (root / ".split-video-cache").glob("*")] if (root / ".split-video-cache").exists() else []
    )
    assert [n for n in leftovers if "proxy" in n or "thumbs" in n] == []
    assert proxy.flags(media, asset) == {"proxy": False, "thumbnails": False}


def test_an_ffmpeg_failure_leaves_nothing_and_says_why(tmp_path):
    (tmp_path / "bad.mp4").write_bytes(b"not a video at all " * 100)
    media = InMemoryMediaStore(materialise=True)
    media.add("bad.mp4", b"not a video at all " * 100, info=MediaInfo(10.0, 320, 240, False, 25.0))
    asset = assets.register_path(media, "bad.mp4")
    with pytest.raises(ExtractError):
        proxy.prepare(media, asset.id)
    assert media.ids() == ["bad.mp4"]


def test_audio_only_assets_are_skipped(tmp_path):
    media = InMemoryMediaStore()
    media.add("song.mp3", b"mp3", info=MediaInfo(30.0, None, None, True))
    asset = assets.register_path(media, "song.mp3")
    result = proxy.prepare(media, asset.id)
    assert result["proxy"] is None and result["made"] == [] and "audio" in result["skipped"]
    assert proxy.flags(media, asset) == {"proxy": False, "thumbnails": False}
    assert proxy.visual_source(media, asset.id) == "song.mp3"
    assert proxy.pending(media) == []


def test_in_memory_store_round_trip(tmp_path):
    _clip(tmp_path / "gig.mp4", seconds=4)
    data = (tmp_path / "gig.mp4").read_bytes()
    media = InMemoryMediaStore(materialise=True)
    media.add("shows/gig.mp4", data, info=MediaInfo(4.0, 640, 360, True, 25.0))
    asset = assets.register_path(media, "shows/gig.mp4")
    assert proxy.pending(media) == [asset.id]
    assert proxy.prepare(media, asset.id)["made"] == ["proxy", "thumbnails"]
    assert proxy.pending(media) == []
    assert media.read("shows/gig.mp4") == data
    assert proxy.visual_source(media, asset.id) == "shows/.derived/gig.mp4.proxy.mp4"
    assert media.read("shows/.derived/gig.mp4.thumbs.jpg")[:2] == b"\xff\xd8"
    assert "shows/.derived/gig.mp4.proxy.mp4" not in [e.path for e in media.list_dir("shows").entries]


# --- keyframes by time, strip tile times (SPL-104) ---------------------------------------------


def _keyframe_times(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0"]
        + [str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(float(row[0]) for row in (line.split(",") for line in out.splitlines()) if "K" in row[1])


def _clock_clip(path, seconds, fps, gop):
    """160x90 video whose frame at second S shows S as ten 16 px bars (white = bit set), so a tile can be
    read back numerically. `gop` is the source's keyframe interval in frames."""
    bars = "255*mod(floor(floor(T)/pow(2,floor(X/16))),2)"
    vf = f"format=yuv420p,geq=lum='{bars}':cb=128:cr=128"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:size=160x90:rate={fps}:duration={seconds}"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-crf", "14", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
    cmd += ["-pix_fmt", "yuv420p", str(path)]
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def _tile_seconds(sprite, meta):
    """The second shown in each tile of the sprite, read from its bars."""
    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(sprite), "-vf", "format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
        check=True,
    ).stdout
    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(meta["height"], meta["width"])
    seconds = []
    for k in range(meta["count"]):
        row, col = divmod(k, meta["columns"])
        y, x0 = row * meta["tile_height"] + meta["tile_height"] // 2, col * meta["tile_width"]
        seconds.append(sum(1 << b for b in range(10) if pixels[y, x0 + 16 * b + 8] > 128))
    return seconds


@pytest.mark.parametrize("fps", [1, 10, 25])
def test_proxy_keyframes_are_two_seconds_apart_at_any_frame_rate(tmp_path, fps):
    _clock_clip(tmp_path / "v.mp4", seconds=61, fps=fps, gop=250)
    media = LocalMediaStore(tmp_path.resolve())
    asset = assets.register_path(media, "v.mp4")
    proxy.prepare(media, asset.id)
    keys = _keyframe_times(tmp_path / ".split-video-cache" / "v.mp4.proxy.mp4")
    gaps = [b - a for a, b in itertools.pairwise(keys)]
    assert len(keys) >= 30 and all(gap == pytest.approx(2.0, abs=0.01) for gap in gaps)


def test_a_version_1_proxy_is_stale_and_rebuilt(local):
    media, asset, root = local
    proxy.prepare(media, asset.id)
    entry = media.load_cache(asset.path, proxy.PROXY_NAME)
    media.save_cache(asset.path, proxy.PROXY_NAME, {**entry, "version": 1})
    media.save_cache(asset.path, proxy.THUMBS_NAME, {**media.load_cache(asset.path, proxy.THUMBS_NAME), "version": 1})
    assert proxy.VERSION > 1
    assert proxy.flags(media, asset) == {"proxy": False, "thumbnails": False}
    assert proxy.pending(media) == [asset.id]
    assert proxy.prepare(media, asset.id)["made"] == ["proxy", "thumbnails"]


def test_strip_tiles_from_the_proxy_of_a_1_fps_file_are_within_a_second(tmp_path):
    _clock_clip(tmp_path / "slow.mp4", seconds=900, fps=1, gop=30)  # 30 s between source keyframes
    media = LocalMediaStore(tmp_path.resolve())
    asset = assets.register_path(media, "slow.mp4")
    meta = proxy.prepare(media, asset.id)["thumbnails"]
    assert (meta["source"], meta["method"], meta["interval"], meta["count"]) == ("proxy", "keyframes", 10.0, 90)
    assert meta["tolerance"] == 1.0
    seconds = _tile_seconds(tmp_path / ".split-video-cache" / "slow.mp4.thumbs.jpg", meta)
    assert max(abs(s - k * meta["interval"]) for k, s in enumerate(seconds)) <= meta["tolerance"]


def test_strip_from_a_sparse_original_seeks_to_each_tile(tmp_path):
    _clock_clip(tmp_path / "slow.mp4", seconds=900, fps=1, gop=30)
    media = LocalMediaStore(tmp_path.resolve())
    asset = assets.register_path(media, "slow.mp4")
    assert proxy.keyframe_gap(str(tmp_path / "slow.mp4"), asset.duration) == pytest.approx(30.0)
    assert proxy.proxy_info(media, asset) is None
    reported = []
    meta = proxy._make_strip(media, asset, "ffmpeg", lambda done, total: reported.append(done), None)
    assert (meta["source"], meta["method"], meta["count"]) == ("original", "seek", 90)
    seconds = _tile_seconds(tmp_path / ".split-video-cache" / "slow.mp4.thumbs.jpg", meta)
    assert seconds == [k * 10 for k in range(90)]  # exact: one frame per second at 1 fps
    assert reported == sorted(reported) and reported[-1] == proxy.STEPS


def test_strip_from_a_dense_original_decodes_keyframes_only(tmp_path):
    _clock_clip(tmp_path / "dense.mp4", seconds=900, fps=2, gop=2)  # a keyframe every second
    media = LocalMediaStore(tmp_path.resolve())
    asset = assets.register_path(media, "dense.mp4")
    assert proxy.keyframe_gap(str(tmp_path / "dense.mp4"), asset.duration) == pytest.approx(1.0)
    meta = proxy._make_strip(media, asset, "ffmpeg", lambda done, total: None, None)
    assert (meta["source"], meta["method"], meta["tolerance"]) == ("original", "keyframes", 0.5)
    seconds = _tile_seconds(tmp_path / ".split-video-cache" / "dense.mp4.thumbs.jpg", meta)
    assert max(abs(s - k * meta["interval"]) for k, s in enumerate(seconds)) <= meta["tolerance"]


def test_keyframe_gap_samples_a_long_file_in_windows(tmp_path):
    _clock_clip(tmp_path / "long.mp4", seconds=1500, fps=1, gop=60)
    assert proxy.keyframe_gap(str(tmp_path / "long.mp4"), 1500.0) == pytest.approx(60.0)


def test_seek_strip_honours_cancel(tmp_path):
    _clock_clip(tmp_path / "slow.mp4", seconds=300, fps=1, gop=30)
    media = LocalMediaStore(tmp_path.resolve())
    asset = assets.register_path(media, "slow.mp4")
    with pytest.raises(JobCancelledError):
        proxy._make_strip(media, asset, "ffmpeg", lambda done, total: None, Cancelled())
    assert proxy.thumbnails_info(media, asset) is None
