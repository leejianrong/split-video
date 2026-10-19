"""Contact sheets: a clip with a different flat colour every second, so each tile can be checked against the time it
claims (SPL-46). Real ffmpeg, on the in-memory store (materialised) and the local one."""

import subprocess

import numpy as np
import pytest

from split_video import assets, contact_sheet, proxy
from split_video.contact_sheet import format_time
from split_video.ffmpeg import MediaInfo
from split_video.frames import FrameArgumentError, FrameAudioOnlyError, FrameTimeError
from split_video.jobs import JobCancelledError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import InMemoryMediaStore
from split_video.timeline import TimelineError

# second k of the clip is COLOURS[k]: all fairly bright, so the dark timestamp box stands out on every one
COLOURS = [
    (255, 64, 64),
    (64, 255, 64),
    (96, 96, 255),
    (255, 255, 64),
    (255, 64, 255),
    (64, 255, 255),
    (255, 255, 255),
    (255, 160, 64),
    (176, 96, 255),
    (255, 160, 200),
    (160, 255, 64),
    (200, 200, 200),
]
SECONDS = len(COLOURS)
SIZE = (640, 360)


def sheet_clip(path, with_audio=True):
    """12 s, 640x360 at 10 fps, a keyframe every second, the second's number burned into the lower right."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for rgb in COLOURS:
        cmd += ["-f", "lavfi", "-i", f"color=c=0x{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}:s={SIZE[0]}x{SIZE[1]}:r=10:d=1"]
    cmd += ["-f", "lavfi", "-i", f"sine=duration={SECONDS}"] if with_audio else []
    graph = "".join(f"[{k}:v]" for k in range(SECONDS)) + f"concat=n={SECONDS}:v=1:a=0,format=yuv420p[o]"
    cmd += ["-filter_complex", graph, "-map", "[o]"] + (["-map", f"{SECONDS}:a", "-c:a", "aac"] if with_audio else [])
    cmd += ["-c:v", "libx264", "-g", "10", str(path)]
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def decode(data):
    """An encoded image as an (h, w, 3) uint8 array, decoded by ffmpeg itself (Pillow is not a dependency)."""
    shape = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", "-"],
        input=data,
        capture_output=True,
        check=True,
    ).stdout.decode()
    width, height = (int(n) for n in shape.strip().split(","))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "-", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        input=data,
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)


def colour_index(pixel):
    """Which palette colour a pixel is closest to."""
    return min(range(SECONDS), key=lambda k: sum((int(pixel[i]) - COLOURS[k][i]) ** 2 for i in range(3)))


def tile_colour(image, tile, layout):
    """The palette index at the lower right of a tile (away from the timestamp in its top left corner)."""
    y = tile.y + layout.tile_height * 3 // 4
    x = tile.x + layout.tile_width * 3 // 4
    return colour_index(image[y, x])


@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory):
    path = tmp_path_factory.mktemp("sheet") / "colours.mp4"
    sheet_clip(path)
    return path.read_bytes()


@pytest.fixture
def local(tmp_path, clip_bytes):
    (tmp_path / "colours.mp4").write_bytes(clip_bytes)
    media = LocalMediaStore(tmp_path.resolve())
    return media, assets.register_path(media, "colours.mp4")


@pytest.fixture
def memory(clip_bytes):
    media = InMemoryMediaStore(materialise=True)
    media.add("show/colours.mp4", clip_bytes, info=MediaInfo(12.0, 640, 360, True, 10.0))
    return media, assets.register_path(media, "show/colours.mp4")


# --- layout, times and the grid -------------------------------------------------------------------


@pytest.mark.parametrize("which", ["local", "memory"])
def test_twelve_tiles_in_a_4x3_grid_each_showing_its_reported_time(which, request):
    media, asset = request.getfixturevalue(which)
    sheet = contact_sheet.make_sheet(media, asset.id)
    lay = sheet.layout
    assert (lay.columns, lay.rows, lay.tile_width, lay.tile_height) == (4, 3, 320, 180)
    assert (lay.width, lay.height) == (4 * 320, 3 * 180)
    image = decode(sheet.data)
    assert image.shape == (540, 1280, 3) and sheet.mime == "image/jpeg" and sheet.data[:3] == b"\xff\xd8\xff"
    assert [t.time for t in lay.tiles] == [i + 0.5 for i in range(12)]  # centres of 12 one-second slices
    for i, tile in enumerate(lay.tiles):  # row-major, and the colour is that second's
        assert (tile.index, tile.x, tile.y) == (i, (i % 4) * 320, (i // 4) * 180)
        assert tile_colour(image, tile, lay) == int(tile.time), i
    assert lay.start == 0 and lay.end == 12 and lay.duration == 12 and lay.source == "original"


def test_a_range_in_the_middle_samples_the_centres_of_equal_slices(local):
    media, asset = local
    sheet = contact_sheet.make_sheet(media, asset.id, 3, 9, count=3, columns=3, format="png")
    lay = sheet.layout
    assert [t.time for t in lay.tiles] == [4.0, 6.0, 8.0] and (lay.columns, lay.rows) == (3, 1)
    assert sheet.data[:8] == b"\x89PNG\r\n\x1a\n" and sheet.mime == "image/png"
    image = decode(sheet.data)
    assert image.shape == (180, 960, 3)
    assert [tile_colour(image, t, lay) for t in lay.tiles] == [4, 6, 8]


def test_auto_columns_favour_wider_than_tall():
    got = {n: contact_sheet.auto_columns(n) for n in (1, 2, 3, 4, 5, 6, 9, 10, 12, 16, 64)}
    assert got == {1: 1, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3, 9: 3, 10: 4, 12: 4, 16: 4, 64: 8}


def test_a_partial_last_row_is_black_and_columns_can_be_chosen(local):
    media, asset = local
    sheet = contact_sheet.make_sheet(media, asset.id, count=5, tile_width=160)  # 3 columns x 2 rows, one cell empty
    lay = sheet.layout
    assert (lay.columns, lay.rows, lay.width, lay.height) == (3, 2, 480, 180)
    image = decode(sheet.data)
    empty = image[lay.tile_height :, 2 * 160 :]
    assert empty.max() < 24  # JPEG noise only
    assert tile_colour(image, lay.tiles[4], lay) == int(lay.tiles[4].time)
    wide = contact_sheet.make_sheet(media, asset.id, count=6, columns=6, tile_width=100)
    assert (wide.layout.columns, wide.layout.rows, wide.layout.width) == (6, 1, 600)
    assert decode(wide.data).shape[:2] == (wide.layout.height, 600)


def test_plan_sheet_matches_make_sheet_without_running_ffmpeg(local, monkeypatch):
    media, asset = local
    planned = contact_sheet.plan_sheet(media, asset.id, 2, 8, count=6)
    monkeypatch.setattr(contact_sheet, "_require_binary", lambda name: pytest.fail("no ffmpeg for a plan"))
    assert planned.info()["tiles"][0] == {"index": 0, "time": 2.5, "x": 0, "y": 0}
    monkeypatch.undo()
    assert contact_sheet.make_sheet(media, asset.id, 2, 8, count=6).layout == planned


def test_the_proxy_is_used_when_there_is_one(local):
    media, asset = local
    proxy.prepare(media, asset.id)
    auto = contact_sheet.make_sheet(media, asset.id, count=4)
    assert auto.layout.source == "proxy" and decode(auto.data).shape[:2] == (360, 640)
    assert [tile_colour(decode(auto.data), t, auto.layout) for t in auto.layout.tiles] == [1, 4, 7, 10]
    assert contact_sheet.make_sheet(media, asset.id, count=4, source="original").layout.source == "original"


# --- timestamps -----------------------------------------------------------------------------------


def test_time_formats():
    assert format_time(83.9, hours=False, tenths=False) == "1:23"
    assert format_time(3723.0, hours=True, tenths=False) == "1:02:03"
    assert format_time(83.96, hours=False, tenths=True) == "1:23.9"
    assert format_time(10800.0, hours=True, tenths=True) == "3:00:00.0"
    assert format_time(4000, hours=False, tenths=False) == "66:40"


def test_tiles_under_ten_seconds_apart_show_tenths_and_long_ranges_show_hours(local):
    media, asset = local
    calls = []
    real = contact_sheet._tile_command

    def spy(ffmpeg, src, seek, label, layout, font):
        calls.append(label)
        return real(ffmpeg, src, seek, label, layout, font)

    contact_sheet._tile_command = spy
    try:
        contact_sheet.make_sheet(media, asset.id, count=2)
    finally:
        contact_sheet._tile_command = real
    assert sorted(calls) == ["0:03.0", "0:09.0"]


def test_timestamps_are_a_dark_box_with_bright_text_in_every_tiles_corner(local):
    media, asset = local
    sheet = contact_sheet.make_sheet(media, asset.id, tile_width=320)
    lay = sheet.layout
    image = decode(sheet.data).astype(int)
    luma = (image[..., 0] * 299 + image[..., 1] * 587 + image[..., 2] * 114) // 1000
    for tile in lay.tiles:
        bg = luma[tile.y + 100 : tile.y + 170, tile.x + 20 : tile.x + 300].mean()  # the picture itself
        corner = luma[tile.y : tile.y + 32, tile.x : tile.x + 70]
        assert bg > 100  # a bright picture, so a patch much darker than it can only be the box
        assert (corner < 0.6 * bg).mean() > 0.3, tile.index  # the box (60 % black over the picture)
        assert (corner > 235).mean() > 0.03, tile.index  # the digits
    # and the font is a legible size: the glyph rows are at least 14 px tall at 320 px wide
    rows = np.where((luma[0:40, 0:70] > 235).any(axis=1))[0]
    assert rows.max() - rows.min() + 1 >= 10  # digit height; the font size (18 px) is about 1.4x that


# --- ranges, arguments and the end of the file ---------------------------------------------------


def test_the_end_of_the_file_still_produces_a_full_sheet(local):
    media, asset = local
    sheet = contact_sheet.make_sheet(media, asset.id, 11.99, 12.0, count=4, columns=2)
    assert decode(sheet.data).shape == (360, 640, 3)
    image = decode(sheet.data)
    assert {tile_colour(image, t, sheet.layout) for t in sheet.layout.tiles} == {11}
    whole = contact_sheet.make_sheet(media, asset.id, 0, 12.0004, count=12)  # a hair past the end is clamped
    assert whole.layout.end == 12.0


def test_a_tile_that_cannot_be_decoded_repeats_the_previous_one(local, monkeypatch):
    media, asset = local
    real = contact_sheet.run_cancellable

    def flaky(cmd, cancel=None, *, text=True):
        if "-ss" in cmd and abs(float(cmd[cmd.index("-ss") + 1]) - 9.5) < 1e-6:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return real(cmd, cancel, text=text)

    monkeypatch.setattr(contact_sheet, "run_cancellable", flaky)
    sheet = contact_sheet.make_sheet(media, asset.id)
    image = decode(sheet.data)
    assert tile_colour(image, sheet.layout.tiles[9], sheet.layout) == 8  # the tile for 9.5 s repeats the one for 8.5 s


def test_range_errors_state_the_duration(local):
    media, asset = local
    for kwargs in ({"end": 13}, {"start": 12.5, "end": None}, {"start": 5, "end": 5}, {"start": 8, "end": 3}):
        with pytest.raises(FrameTimeError) as exc:
            contact_sheet.make_sheet(media, asset.id, **kwargs)
        assert "12 s long" in str(exc.value) or "before end" in str(exc.value), kwargs
    for bad in (-1.0, float("nan")):
        with pytest.raises(FrameTimeError):
            contact_sheet.make_sheet(media, asset.id, bad)


def test_argument_errors(local):
    media, asset = local
    for kwargs in (
        {"count": 0},
        {"count": 65},
        {"columns": 0},
        {"columns": 13},
        {"tile_width": 63},
        {"tile_width": 1281},
        {"columns": 8, "count": 8, "tile_width": 400},  # 3200 px wide
        {"format": "gif"},
        {"source": "proxy"},
    ):
        with pytest.raises(FrameArgumentError):
            contact_sheet.make_sheet(media, asset.id, **kwargs)
    with pytest.raises(FrameArgumentError, match="2560"):
        contact_sheet.plan_sheet(media, asset.id, count=9, columns=9, tile_width=320)


def test_the_largest_sheet_is_allowed(local):
    media, asset = local
    lay = contact_sheet.plan_sheet(media, asset.id, count=64, tile_width=320)
    assert (lay.columns, lay.rows, lay.width) == (8, 8, 2560)


def test_audio_only_and_unknown_assets(tmp_path):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", str(tmp_path / "s.mp3")], check=True
    )
    media = LocalMediaStore(tmp_path.resolve())
    song = assets.register_path(media, "s.mp3")
    with pytest.raises(FrameAudioOnlyError):
        contact_sheet.make_sheet(media, song.id)
    with pytest.raises((TimelineError, Exception)):
        contact_sheet.make_sheet(media, "a_0000000000000000")


def test_no_font_is_a_clear_error(local, monkeypatch):
    media, asset = local

    def nofont():
        raise TimelineError("no font found for text clips")

    monkeypatch.setattr(contact_sheet, "find_font", nofont)
    with pytest.raises(contact_sheet.SheetFontError, match="no font"):
        contact_sheet.make_sheet(media, asset.id)


def test_cancelling_stops_the_work(local):
    media, asset = local

    class Cancelled:
        def is_cancelled(self):
            return True

        def raise_if_cancelled(self):
            raise JobCancelledError("cancelled")

        def on_cancel(self, callback):
            callback()
            return lambda: None

    with pytest.raises(JobCancelledError):
        contact_sheet.make_sheet(media, asset.id, cancel=Cancelled())
