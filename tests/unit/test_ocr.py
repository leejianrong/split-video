"""On-screen text: the frame schedule, change detection, storage and currency, progress, cancel, parallelism, search
and paging, and the missing-binary hint, with a fake engine and fake frames on the in-memory store (SPL-98). The real
tesseract on rendered screens is in test_ocr_real.py."""

import shutil
import threading

import numpy as np
import pytest
from fake_ocr import FakeEngine, install_frames

from split_video import assets, ocr, scenes
from split_video.ffmpeg import ExtractError, MediaInfo
from split_video.jobs import JobCancelledError
from split_video.media_store import InMemoryMediaStore

# 100 s of screen: an editor, a terminal from 40 s, a slide from 70 s
SCREENS = [
    (0.0, "# scenes.py\ndef merge_cuts(scenes, min_gap):\n    kept = strongest_of(scenes)"),
    (
        40.0,
        (
            "$ make test\nFAILED tests/unit/test_scenes.py::test_cut_burst_keeps_strongest - AssertionError\n"
            "E       AssertionError: assert 3 == 2"
        ),
    ),
    (70.0, "Building a video editor for agents\nChapter two: silence detection"),
]


@pytest.fixture
def memory():
    media = InMemoryMediaStore()
    media.add("show/screen.mp4", b"video", info=MediaInfo(100.0, 1920, 1080, False, 5.0))
    return media, assets.register_path(media, "show/screen.mp4")


@pytest.fixture
def engine(monkeypatch):
    install_frames(monkeypatch)
    return FakeEngine(SCREENS)


def with_scenes(media, asset, times, kind="cut"):
    media.save_cache(
        asset.path,
        scenes.CACHE_NAME,
        {
            "version": scenes.VERSION,
            "asset": asset.id,
            "source": "original",
            "duration": 100.0,
            "threshold": 0.15,
            "min_gap": 1.0,
            "screen_threshold": 0.02,
            "sample_fps": 5.0,
            "activity_interval": 1.0,
            "scenes": [
                {"time": t, "score": 0.5 if kind == "cut" else 0.01, "kind": kind, "changed": 0.06} for t in times
            ],
            "activity": [0.0] * 100,
        },
    )


# --- the schedule ---------------------------------------------------------------------------------


def times(plan):
    return [t for t, _ in plan.frames]


def test_interval_only_when_there_are_no_scenes():
    plan = ocr.schedule(100.0, [], 30.0)
    assert plan.frames == [(0.0, False), (30.0, False), (60.0, False), (90.0, False)]
    assert plan.before_cap == 4 and plan.capped is False


def test_scene_frames_come_half_a_second_after_the_cut_and_merge_with_the_interval():
    plan = ocr.schedule(100.0, [10.0, 40.0], 30.0)
    assert plan.frames == [(0.0, False), (10.5, True), (30.0, False), (40.5, True), (60.0, False), (90.0, False)]


def test_frames_closer_than_the_min_gap_are_merged_and_the_scene_frame_wins():
    plan = ocr.schedule(100.0, [29.0, 30.1], 30.0)  # 29.5 and 30.6 around the interval frame at 30
    assert (29.5, True) in plan.frames and (30.0, False) not in plan.frames
    assert all(b - a >= ocr.MIN_GAP for a, b in zip(times(plan), times(plan)[1:], strict=False))
    close = ocr.schedule(100.0, [0.4, 0.9, 1.2], 30.0)  # a burst of cuts: one frame
    assert [t for t in times(close) if t < 5] == [0.9]
    assert ocr.schedule(100.0, [], 30.0, min_gap=0.0).frames == ocr.schedule(100.0, [], 30.0).frames


def test_text_only_screen_changes_are_sampled_like_cuts(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    engine = FakeEngine(SCREENS)
    with_scenes(media, asset, [40.0, 70.0], kind="screen")  # found by the screen rule, not scdet
    made = ocr.recognize(media, asset.id, interval=50.0, engine=engine)
    assert made["scenes_used"] is True and made["scene_frames"] == 2
    assert [(f["time"], f["source_scene"]) for f in ocr.load(media, asset.id)["frames"]][:3] == [
        (0.0, False),
        (40.5, True),
        (50.0, False),
    ]


def test_a_scene_at_the_very_end_is_clamped_inside_the_asset():
    plan = ocr.schedule(100.0, [99.9], 30.0)
    assert max(times(plan)) <= 99.95


def test_the_cap_thins_interval_frames_first_and_keeps_every_scene_frame():
    scenes_at = [20.0 * k for k in range(1, 5)]  # 4 scene frames
    plan = ocr.schedule(1000.0, scenes_at, 10.0, max_frames=20)
    assert plan.capped is True and plan.before_cap > 90 and len(plan.frames) == 20
    assert sum(1 for _, s in plan.frames if s) == 4
    spread = [t for t, s in plan.frames if not s]
    assert spread[0] == 0.0 and spread[-1] > 900  # spread over the whole recording, not the first minutes


def test_the_cap_thins_scene_frames_too_when_they_alone_exceed_it():
    plan = ocr.schedule(1000.0, [10.0 * k for k in range(1, 90)], 500.0, max_frames=10)
    assert len(plan.frames) == 10 and plan.capped and all(s for _, s in plan.frames)
    assert times(plan) == sorted(times(plan))


def test_a_short_asset_still_gets_a_frame():
    assert ocr.schedule(0.2, [], 30.0).frames == [(0.0, False)]


# --- text and change detection --------------------------------------------------------------------


def test_normalise_strips_control_characters_and_trailing_space_and_blank_lines():
    text, lines = ocr.normalise("  a = 1  \r\n\x00\x07\n\n\tb\x0c\nlast\x1b   \n\n")
    assert lines == ["  a = 1", "    b", "last"] and text == "  a = 1\n    b\nlast"
    assert ocr.normalise("") == ("", []) and ocr.normalise("\x00 \n") == ("", [])


def test_normalise_caps_the_text_at_a_line():
    raw = "\n".join(f"line {k:04d} " + "x" * 40 for k in range(500))
    text, lines = ocr.normalise(raw)
    assert len(text) <= ocr.MAX_TEXT_CHARS and 50 < len(lines) < 500 and text == "\n".join(lines)
    assert len(ocr.normalise("y" * 9000)[0]) == ocr.MAX_TEXT_CHARS


def signature(fn=None):
    plane = np.full((ocr.SIGNATURE_HEIGHT, ocr.SIGNATURE_WIDTH), 30, np.uint8)
    if fn:
        fn(plane)
    return plane.tobytes()


def test_a_cursor_does_not_change_the_frame_but_a_new_line_does():
    base = signature()

    def cursor(p):
        p[50, 40] = 255  # a few signature pixels: a blinking cursor
        p[51, 40] = 255

    def digit(p):
        p[50:55, 40:44] = 200  # one changed digit or short word: 20 pixels

    def line(p):
        p[50, 10:90] = 120  # a line of text, 80 pixels wide

    def noise(p):
        p[::7, ::5] += 15  # compression noise: lots of pixels, each under the difference bar

    assert ocr.has_changed(base, signature(cursor)) is False
    assert ocr.has_changed(base, signature(noise)) is False
    assert ocr.has_changed(base, signature(digit)) is True
    assert ocr.has_changed(base, signature(line)) is True
    assert ocr.has_changed(base, base) is False
    assert ocr.has_changed(base, b"short") is True


def test_dark_pictures_are_inverted_light_ones_are_not():
    assert ocr.is_dark(bytes([20]) * 100) and not ocr.is_dark(bytes([240]) * 100) and not ocr.is_dark(bytes([128]) * 10)


# --- recognize on the in-memory store ------------------------------------------------------------


def test_recognize_stores_the_document_and_reuses_it(memory, monkeypatch):
    media, asset = memory
    calls = []
    install_frames(monkeypatch, calls=calls)
    engine = FakeEngine(SCREENS)
    first = ocr.recognize(media, asset.id, interval=10.0, engine=engine)
    assert first == {
        "version": 1,
        "asset": asset.id,
        "engine": "fake-ocr 1.0",
        "duration": 100.0,
        "interval": 10.0,
        "max_frames": 2000,
        "scenes_used": False,
        "frames": 10,
        "scene_frames": 0,
        "ocr_frames": 3,
        "skipped_unchanged": 7,
        "capped": False,
        "frames_before_cap": 10,
        "failed_frames": 0,
        "note": first["note"],
        "made": True,
    }
    assert "no scene data" in first["note"] and "scenes first" in first["note"]
    assert all(c["width"] == 1280 and c["format"] == "png" and c["source"] == "original" for c in calls)

    doc = ocr.load(media, asset.id)
    assert doc["version"] == 1 and doc["engine"] == "fake-ocr 1.0" and doc["interval"] == 10.0
    by_time = {f["time"]: f for f in doc["frames"]}
    assert [f["changed"] for f in doc["frames"]] == [True, False, False, False, True, False, False, True, False, False]
    assert by_time[0.0]["lines"][1] == "def merge_cuts(scenes, min_gap):" and by_time[0.0]["until"] == 40.0
    assert by_time[10.0] == {
        "time": 10.0,
        "text": "",
        "lines": [],
        "changed": False,
        "source_scene": False,
        "reused_from": 0.0,
    }
    assert by_time[40.0]["until"] == 70.0 and by_time[70.0]["until"] == 100.0 and by_time[90.0]["reused_from"] == 70.0
    assert ocr.has_ocr(media, asset) and sorted(engine.reads) == [0.0, 40.0, 70.0]

    again = ocr.recognize(media, asset.id, interval=10.0, engine=engine)
    assert again["made"] is False and len(engine.reads) == 3  # current: nothing read again
    assert ocr.recognize(media, asset.id, interval=10.0, engine=engine, force=True)["made"] is True
    assert ocr.recognize(media, asset.id, interval=20.0, engine=engine)["made"] is True  # other settings redo it
    assert ocr.recognize(media, asset.id, interval=20.0, max_frames=3, engine=engine)["made"] is True


def test_scene_frames_are_read_when_scenes_exist_and_a_later_scene_document_redoes_it(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    engine = FakeEngine(SCREENS)
    plain = ocr.recognize(media, asset.id, interval=50.0, engine=engine)
    assert plain["scenes_used"] is False and plain["scene_frames"] == 0
    assert [f["time"] for f in ocr.load(media, asset.id)["frames"]] == [0.0, 50.0]

    with_scenes(media, asset, [40.0, 70.0])
    better = ocr.recognize(media, asset.id, interval=50.0, engine=engine)
    assert better["made"] is True and better["scenes_used"] is True and better["scene_frames"] == 2
    assert better["note"] is None
    doc = ocr.load(media, asset.id)
    assert [(f["time"], f["source_scene"]) for f in doc["frames"]] == [
        (0.0, False),
        (40.5, True),
        (50.0, False),
        (70.5, True),
    ]
    assert [f["changed"] for f in doc["frames"]] == [True, True, False, True]
    assert ocr.recognize(media, asset.id, interval=50.0, engine=engine)["made"] is False


def test_the_cap_is_reported_with_a_note(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    result = ocr.recognize(media, asset.id, interval=1.0, max_frames=10, engine=FakeEngine(SCREENS))
    assert result["capped"] is True and result["frames"] == 10 and result["frames_before_cap"] == 50
    assert "capped at 10 of 50" in result["note"] and "max_frames" in result["note"]


def test_a_changed_file_or_version_is_stale(memory, engine):
    media, asset = memory
    ocr.recognize(media, asset.id, interval=30.0, engine=engine)
    assert ocr.has_ocr(media, asset) and ocr.load(media, asset.id)
    stored = media.load_cache(asset.path, ocr.CACHE_NAME)
    media.save_cache(asset.path, ocr.CACHE_NAME, {**stored, "asset": "a_other"})
    assert ocr.load(media, asset.id) is None
    media.save_cache(asset.path, ocr.CACHE_NAME, {**stored, "version": 0})
    assert ocr.load(media, asset.id) is None
    media.save_cache(asset.path, ocr.INFO_NAME, {"version": 0, "asset": asset.id})
    assert not ocr.has_ocr(media, asset)
    assert ocr.recognize(media, asset.id, interval=30.0, engine=engine)["made"] is True


def test_progress_counts_frames_and_ends_at_the_total(memory, engine):
    media, asset = memory
    seen = []
    ocr.recognize(media, asset.id, interval=10.0, engine=engine, on_progress=lambda d, t: seen.append((d, t)))
    assert seen[0] == (0, 10) and seen[-1] == (10, 10)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen) and {t for _, t in seen} == {10}


def test_cancel_stops_between_frames_and_stores_nothing(memory, engine):
    media, asset = memory

    class CancelAfter:
        def __init__(self, calls):
            self.calls = calls

        def raise_if_cancelled(self):
            self.calls -= 1
            if self.calls < 0:
                raise JobCancelledError("job cancelled")

        def on_cancel(self, callback):
            return lambda: None

    with pytest.raises(JobCancelledError):
        ocr.recognize(media, asset.id, interval=2.0, engine=engine, cancel=CancelAfter(5))
    assert ocr.load(media, asset.id) is None and not ocr.has_ocr(media, asset)
    assert len(engine.reads) < 50  # nowhere near all 50 frames were read


def test_a_failing_frame_is_left_out_but_not_fatal_and_all_failing_is(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch, fail_at=[10.0])
    result = ocr.recognize(media, asset.id, interval=10.0, engine=FakeEngine(SCREENS))
    assert result["frames"] == 9 and result["failed_frames"] == 1 and "could not be decoded" in result["note"]
    assert 10.0 not in [f["time"] for f in ocr.load(media, asset.id)["frames"]]
    install_frames(monkeypatch, fail_at=[0.0, 30.0, 60.0, 90.0])
    with pytest.raises(Exception, match="no frame"):
        ocr.recognize(media, asset.id, interval=30.0, engine=FakeEngine(SCREENS), force=True)


def test_an_engine_error_stores_nothing_and_keeps_the_old_text(memory, engine, monkeypatch):
    media, asset = memory
    ocr.recognize(media, asset.id, interval=30.0, engine=engine)
    before = ocr.load(media, asset.id)

    class Broken(FakeEngine):
        def read(self, image, *, time, dark, cancel):
            raise ocr.OcrError("tesseract failed at 0 s: boom")

    with pytest.raises(ocr.OcrError, match="boom"):
        ocr.recognize(media, asset.id, interval=30.0, engine=Broken(SCREENS), force=True)
    assert ocr.load(media, asset.id) == before


def test_at_most_four_frames_are_in_flight_and_progress_keeps_working(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    # every frame different so each is read; a slow engine makes them overlap
    screens = [(float(t), f"screen number {t}") for t in range(0, 100, 2)]
    engine = FakeEngine(screens, delay=0.05)
    result = ocr.recognize(media, asset.id, interval=2.0, engine=engine)
    assert result["ocr_frames"] == 50 and 2 <= engine.peak <= ocr.WORKERS
    doc = ocr.load(media, asset.id)
    assert [f["time"] for f in doc["frames"]] == [float(t) for t in range(0, 100, 2)]  # in time order
    assert all(f["text"] == f"screen number {int(f['time'])}" for f in doc["frames"])


def test_the_dark_flag_follows_the_signature(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    engine = FakeEngine([(0.0, "dark one"), (50.0, "bright one")])
    ocr.recognize(media, asset.id, interval=50.0, engine=engine)
    assert engine.darks == [True, True]  # fake planes are mostly 20: both under 128


def test_audio_only_assets_are_skipped_and_bad_arguments_refused(memory, engine):
    media, asset = memory
    media.add("song.mp3", b"a", info=MediaInfo(3.0, None, None, True))
    song = assets.register_path(media, "song.mp3")
    assert ocr.recognize(media, song.id, engine=engine) == {
        "asset": song.id,
        "skipped": "audio-only asset",
        "made": False,
    }
    for bad in (
        {"interval": 0.5},
        {"interval": float("nan")},
        {"max_frames": 0},
        {"max_frames": 10**6},
        {"max_frames": 2.5},
    ):
        with pytest.raises(ocr.OcrArgumentError):
            ocr.recognize(media, asset.id, engine=engine, **bad)
    assert not ocr.has_ocr(media, asset) and engine.reads == []


# --- the missing program ---------------------------------------------------------------------------


def test_without_tesseract_everything_that_needs_it_raises_the_hint(memory, monkeypatch):
    media, asset = memory
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert ocr.available() is False
    with pytest.raises(ocr.OcrUnavailableError) as raised:
        ocr.require_available()
    message = str(raised.value)
    assert message.startswith(ocr.UNAVAILABLE) and "apt install tesseract-ocr" in message and "WITH_OCR=1" in message
    monkeypatch.setattr(ocr.frames, "extract_frame", lambda *a, **k: pytest.fail("no frame may be decoded"))
    with pytest.raises(ocr.OcrUnavailableError):
        ocr.recognize(media, asset.id)
    assert ocr.load(media, asset.id) is None


def test_reading_is_possible_without_tesseract(memory, engine, monkeypatch):
    media, asset = memory
    ocr.recognize(media, asset.id, interval=10.0, engine=engine)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert ocr.ocr_search(media, asset.id, "merge cuts")["total_matches"] == 1
    assert ocr.ocr_range(media, asset.id)["total_in_range"] == 3


# --- the tesseract engine, with the subprocess calls faked -----------------------------------------


class Recorder:
    def __init__(self, tesseract_stdout="text", tesseract_code=0, tesseract_stderr=""):
        self.commands = []
        self.envs = []
        self.files = []
        self.tesseract = (tesseract_stdout, tesseract_code, tesseract_stderr)

    def __call__(self, cmd, cancel=None, *, text=True, env=None):
        import subprocess

        self.commands.append(cmd)
        self.envs.append(env)
        if cmd[0] == "ffmpeg-bin":
            if "rawvideo" in cmd:
                return subprocess.CompletedProcess(cmd, 0, bytes(ocr.SIGNATURE_WIDTH * ocr.SIGNATURE_HEIGHT), b"")
            self.files.append(cmd[cmd.index("-i") + 1])
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        out, code, err = self.tesseract
        return subprocess.CompletedProcess(cmd, code, out, err)


def test_the_tesseract_engine_prepares_with_ffmpeg_then_reads_with_psm_6(monkeypatch):
    rec = Recorder("hello\n")
    monkeypatch.setattr(ocr, "run_cancellable", rec)
    monkeypatch.setattr(ocr, "_version_cache", {"/usr/bin/tesseract": "tesseract 5.5.0"})
    engine = ocr.TesseractEngine("/usr/bin/tesseract", "ffmpeg-bin")
    assert engine.name == "tesseract 5.5.0"
    assert engine.read(b"png", time=3.0, dark=True, cancel=None) == "hello\n"
    prep, run = rec.commands
    graph = prep[prep.index("-filter_complex") + 1]
    assert prep[prep.index("-map") + 1] == "[o]" and graph == ocr.prepare_graph(True)
    assert "all_mode=lighten" in graph and "negate" in graph and graph.endswith("scale=iw*2:ih*2:flags=lanczos[o]")
    assert run[0] == "/usr/bin/tesseract" and run[2] == "stdout" and run[3:] == ["--psm", "6", "-l", "eng"]
    assert rec.envs[1]["OMP_THREAD_LIMIT"] == "1"
    engine.read(b"png", time=3.0, dark=False, cancel=None)
    light = rec.commands[2]
    light_graph = light[light.index("-filter_complex") + 1]
    assert "negate" not in light_graph and "all_mode=darken" in light_graph and "lighten" not in light_graph
    assert len(engine.signature(b"png", None)) == ocr.SIGNATURE_WIDTH * ocr.SIGNATURE_HEIGHT
    sig = rec.commands[-1]
    assert sig[sig.index("-vf") + 1].startswith("scale=384:216:flags=area,format=gray")


def test_the_engine_keeps_its_files_in_a_temp_directory_that_goes_away(monkeypatch, tmp_path):
    import os

    rec = Recorder()
    monkeypatch.setattr(ocr, "run_cancellable", rec)
    monkeypatch.setattr(ocr, "_version_cache", {"t": "tesseract 5.5.0"})
    ocr.TesseractEngine("t", "ffmpeg-bin").read(b"png", time=0.0, dark=False, cancel=None)
    source = rec.files[0]
    assert os.path.basename(os.path.dirname(source)).startswith("ocr-") and not os.path.exists(source)
    assert not str(tmp_path) in source


def test_tesseract_failures_are_typed(monkeypatch):
    monkeypatch.setattr(ocr, "_version_cache", {"t": "tesseract 5.5.0"})
    monkeypatch.setattr(
        ocr, "run_cancellable", Recorder(tesseract_code=1, tesseract_stderr="Failed loading language 'eng'")
    )
    with pytest.raises(ocr.OcrUnavailableError, match="tesseract-ocr-eng"):
        ocr.TesseractEngine("t", "ffmpeg-bin").read(b"png", time=1.0, dark=False, cancel=None)
    monkeypatch.setattr(ocr, "run_cancellable", Recorder(tesseract_code=1, tesseract_stderr="Segmentation fault"))
    with pytest.raises(ocr.OcrError, match="Segmentation fault"):
        ocr.TesseractEngine("t", "ffmpeg-bin").read(b"png", time=1.0, dark=False, cancel=None)


def test_the_engine_never_imports_a_python_ocr_package():
    import split_video.ocr as module

    with open(module.__file__) as f:
        text = f.read()
    for name in ("pytesseract", "rapidocr", "easyocr", "PIL"):
        assert f"import {name}" not in text and f"from {name}" not in text


# --- reading and searching the stored text -------------------------------------------------------


@pytest.fixture
def stored(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    ocr.recognize(media, asset.id, interval=10.0, engine=FakeEngine(SCREENS))
    return media, asset


def test_ocr_between_gives_the_text_up_in_a_span_including_what_began_earlier(stored):
    media, asset = stored
    spans = {
        (None, None): [0.0, 40.0, 70.0],
        (45.0, 50.0): [40.0],  # began at 40, still up
        (0.0, 40.0): [0.0],  # 40 is excluded: the end is open
        (39.0, 41.0): [0.0, 40.0],
        (95.0, None): [70.0],
    }
    for (start, end), expected in spans.items():
        got = ocr.ocr_between(media, asset.id, start, end)
        assert [s["time"] for s in got] == expected, (start, end)
    first = ocr.ocr_between(media, asset.id, 0, 1)[0]
    assert set(first) == {"time", "until", "text", "lines", "source_scene"} and first["until"] == 40.0
    with pytest.raises(ocr.OcrQueryError):
        ocr.ocr_between(media, asset.id, 5, 5)


def test_excerpt_cuts_the_document_to_a_span(stored):
    media, asset = stored
    doc = ocr.load(media, asset.id)
    assert ocr.excerpt(doc) is doc
    cut = ocr.excerpt(doc, 45.0, 62.0)
    assert [f["time"] for f in cut["frames"]] == [
        40.0,
        50.0,
        60.0,
    ]  # the source frame that is still up, then two reused
    assert cut["frames"][0]["changed"] and cut["engine"] == doc["engine"]
    assert [f["time"] for f in ocr.excerpt(doc, 0.0, 5.0)["frames"]] == [0.0]


def test_ocr_range_pages_every_state_exactly_once(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    screens = [(float(t), f"state {t}") for t in range(0, 100, 5)]
    ocr.recognize(media, asset.id, interval=5.0, engine=FakeEngine(screens))
    seen, cursor, pages = [], None, 0
    while True:
        page = ocr.ocr_range(media, asset.id, limit=7, cursor=cursor)
        assert page["total_in_range"] == 20 and page["count"] == len(page["items"]) <= 7
        seen += [i["time"] for i in page["items"]]
        cursor, pages = page["next_cursor"], pages + 1
        if not cursor:
            break
    assert seen == [float(t) for t in range(0, 100, 5)] and pages == 3
    window = ocr.ocr_range(media, asset.id, 20.0, 41.0, limit=2)
    assert [i["time"] for i in window["items"]] == [20.0, 25.0] and window["range"] == {"start": 20.0, "end": 41.0}
    rest = ocr.ocr_range(media, asset.id, 20.0, 41.0, limit=2, cursor=window["next_cursor"])
    assert [i["time"] for i in rest["items"]] == [30.0, 35.0]
    with pytest.raises(ocr.OcrQueryError, match="different"):
        ocr.ocr_range(media, asset.id, 0.0, 41.0, limit=2, cursor=window["next_cursor"])


def test_cursor_and_argument_errors(stored):
    media, asset = stored
    for kwargs in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"cursor": "nonsense"}, {"cursor": "e30"}):
        with pytest.raises(ocr.OcrQueryError):
            ocr.ocr_range(media, asset.id, **kwargs)
    for query in ("", "!!! ___", "x" * 501):
        with pytest.raises(ocr.OcrQueryError):
            ocr.ocr_search(media, asset.id, query)
    with pytest.raises(ocr.OcrQueryError):
        ocr.ocr_search(media, asset.id, "x", context=11)
    with pytest.raises(ocr.OcrQueryError, match="negative"):
        ocr.ocr_search(media, asset.id, "x", start=-1)
    page = ocr.ocr_range(media, asset.id, limit=1)
    with pytest.raises(ocr.OcrQueryError, match="different kind"):
        ocr.ocr_search(media, asset.id, "scenes", cursor=page["next_cursor"])


def test_no_stored_text_is_a_typed_error_naming_every_surface(memory):
    media, asset = memory
    for call in (
        lambda: ocr.ocr_search(media, asset.id, "x"),
        lambda: ocr.ocr_range(media, asset.id),
        lambda: ocr.ocr_between(media, asset.id),
    ):
        with pytest.raises(ocr.OcrNotFoundError) as raised:
            call()
        text = str(raised.value)
        assert "split-video ocr" in text and "ocr_asset" in text and "POST /api/v1/assets/{id}/ocr" in text
    with pytest.raises(Exception, match="unknown asset"):
        ocr.ocr_search(media, "a_0000000000000000", "x")


def test_search_finds_a_planted_identifier_with_its_time_and_best_line(stored):
    media, asset = stored
    found = ocr.ocr_search(media, asset.id, "test_cut_burst_keeps_strongest")
    assert found["total_matches"] == 1 and found["count"] == 1 and found["next_cursor"] is None
    assert (
        found["tokens"] == ["test", "cut", "burst", "keeps", "strongest"]
        and found["query"] == "test_cut_burst_keeps_strongest"
    )
    hit = found["hits"][0]
    assert hit["source"] == "ocr" and hit["time"] == 40.0 and hit["end"] == 70.0 and hit["score"] == 1.0
    assert hit["text"].startswith("FAILED tests/unit/test_scenes.py::test_cut_burst_keeps_strongest")
    assert hit["context_before"] == "$ make test" and hit["context_after"].startswith("E ")
    assert (
        hit["matched_tokens"] == ["test", "cut", "burst", "keeps", "strongest"] or "strongest" in hit["matched_tokens"]
    )
    assert hit["frame"] == 4 and ocr.load(media, asset.id)["frames"][hit["frame"]]["time"] == 40.0
    assert found["range"] == {"start": 0.0, "end": None}


def test_search_survives_the_way_tesseract_garbles_identifiers(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    screens = [
        (0.0, "FAILED tests/unit/test_scenes.py::test_cut_burst_ keeps strongest - AssertionError"),  # underscore split
        (30.0, "FAILED tests/unit/test_scenes.py::test_cut_burst_keepsstrongest - Assertion"),  # glued
        (60.0, "FAILED tests/unit/test_scenes.py::test_cut_burst_keeps_strongesl - Assertion"),  # misread letter
        (90.0, "something else entirely: nothing relevant"),
    ]
    ocr.recognize(media, asset.id, interval=30.0, engine=FakeEngine(screens))
    found = ocr.ocr_search(media, asset.id, "test_cut_burst_keeps_strongest")
    assert [h["time"] for h in found["hits"]] == [0.0, 60.0, 30.0]  # exact, one letter off, glued words
    assert [h["score"] for h in found["hits"]] == sorted((h["score"] for h in found["hits"]), reverse=True)
    assert found["hits"][0]["score"] == 1.0 and found["hits"][-1]["score"] < 1.0
    assert ocr.ocr_search(media, asset.id, "KEEPS Strongest")["hits"][0]["time"] == 0.0  # case-insensitive


def test_all_query_tokens_must_be_in_the_frame(stored):
    media, asset = stored
    assert ocr.ocr_search(media, asset.id, "merge_cuts banana")["total_matches"] == 0
    assert ocr.ocr_search(media, asset.id, "merge_cuts min_gap")["total_matches"] == 1
    assert ocr.ocr_search(media, asset.id, "silence agents")["total_matches"] == 1  # two lines, same frame
    assert ocr.ocr_search(media, asset.id, "zebra")["hits"] == []


def test_adjacent_tokens_in_order_outrank_scattered_ones_and_ties_go_by_time(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    screens = [
        (0.0, "alpha beta\nunrelated"),  # adjacent, in order
        (20.0, "beta alpha"),  # same line, wrong order
        (40.0, "alpha\nsomething\nbeta"),  # different lines
        (60.0, "alpha beta"),  # adjacent again: ties with time 0
        (80.0, "alpha beta gamma"),
    ]
    ocr.recognize(media, asset.id, interval=20.0, engine=FakeEngine(screens))
    hits = ocr.ocr_search(media, asset.id, "alpha beta")["hits"]
    assert [h["time"] for h in hits] == [0.0, 60.0, 80.0, 20.0, 40.0]
    assert [h["score"] for h in hits] == [1.0, 1.0, 1.0, 0.875, 0.75]
    assert hits[3]["matched_tokens"] == ["beta", "alpha"] and hits[4]["text"] in ("alpha", "beta")


def test_search_matches_words_split_or_digits_split_by_the_ocr(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    screens = [
        (0.0, "src/module _077/handl er.py\nFAILED test_case 07/7 - AssertionError"),
        (30.0, "src/module_078/handler.py\nFAILED test_case_078"),
        (60.0, "nothing here"),
    ]
    ocr.recognize(media, asset.id, interval=30.0, engine=FakeEngine(screens))
    found = ocr.ocr_search(media, asset.id, "test_case_077")
    assert [h["time"] for h in found["hits"]] == [0.0] and 0.7 < found["hits"][0]["score"] < 1.0
    assert found["hits"][0]["matched_tokens"][:2] == ["test", "case"]
    assert [h["time"] for h in ocr.ocr_search(media, asset.id, "handler")["hits"]] == [30.0, 0.0]
    assert ocr.ocr_search(media, asset.id, "test_case_099")["hits"] == []


def test_search_pages_exactly_once_across_pages_and_filters_by_range(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    screens = [(float(t), f"file_{t % 3}.py needle {t}\nnoise {t}") for t in range(0, 100, 4)]
    ocr.recognize(media, asset.id, interval=4.0, engine=FakeEngine(screens))
    whole = ocr.ocr_search(media, asset.id, "needle", limit=200)
    assert whole["total_matches"] == 25
    seen, cursor = [], None
    while True:
        page = ocr.ocr_search(media, asset.id, "needle", limit=4, cursor=cursor)
        assert page["total_matches"] == 25 and page["count"] <= 4
        seen += [(h["time"], h["frame"]) for h in page["hits"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 25 and sorted(seen) == sorted((h["time"], h["frame"]) for h in whole["hits"])
    window = ocr.ocr_search(media, asset.id, "needle", start=30.0, end=50.0, limit=200)
    assert sorted(h["time"] for h in window["hits"]) == [28.0, 32.0, 36.0, 40.0, 44.0, 48.0]  # 28 is still up at 30
    assert window["range"] == {"start": 30.0, "end": 50.0}
    single = ocr.ocr_search(media, asset.id, "file_1 needle", context=0)["hits"][0]
    assert single["context_before"] == "" and single["context_after"] == ""
    assert ocr.ocr_search(media, asset.id, "file_1 needle", context=5)["hits"][0]["context_after"].startswith("noise")


def test_hits_share_the_fields_a_combined_search_merges_on(stored):
    media, asset = stored
    hit = ocr.ocr_search(media, asset.id, "silence")["hits"][0]
    assert {"source", "time", "end", "score", "text"} <= set(hit) and hit["source"] == "ocr"
    assert 0.0 <= hit["score"] <= 1.0 and isinstance(hit["text"], str)


def test_the_search_index_is_not_cached_across_a_redo(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    ocr.recognize(media, asset.id, interval=50.0, engine=FakeEngine([(0.0, "first text")]))
    assert ocr.ocr_search(media, asset.id, "first")["total_matches"] == 1
    ocr.recognize(media, asset.id, interval=50.0, engine=FakeEngine([(0.0, "second text")]), force=True)
    assert ocr.ocr_search(media, asset.id, "first")["total_matches"] == 0
    assert ocr.ocr_search(media, asset.id, "second")["total_matches"] == 1


def test_two_jobs_for_one_asset_run_one_after_the_other(memory, monkeypatch):
    media, asset = memory
    install_frames(monkeypatch)
    engine = FakeEngine(SCREENS, delay=0.05)
    results = []

    def run():
        results.append(ocr.recognize(media, asset.id, interval=30.0, engine=engine)["made"])

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [False, True]  # the second found the first one's document current


def test_a_failed_extract_error_type_propagates_when_nothing_decodes(memory, monkeypatch):
    media, asset = memory

    def boom(*a, **k):
        raise ExtractError("could not extract a frame")

    monkeypatch.setattr(ocr.frames, "extract_frame", boom)
    with pytest.raises(ExtractError):
        ocr.recognize(media, asset.id, interval=50.0, engine=FakeEngine(SCREENS))
