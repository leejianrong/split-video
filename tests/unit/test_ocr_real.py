"""The real tesseract on rendered 1920x1080 screens with text that is known exactly (SPL-98). Skipped when the
`tesseract` program or a monospace font is missing, so the default CI run, which installs neither, stays fast.

Measured on tesseract 4.1.1 (this machine): token recall 1.00 on all three screens (30 px monospace code on dark,
a red failing-test line and white lines on a mid-grey terminal, 72 px text on a light slide). On the grey terminal
plain luma graying read only 0.53 of the tokens (the red line vanished); `ocr.prepare_graph`'s channel max reads 1.00.
The planted identifier is found at the first frame sampled after it appeared."""

import re
import shutil
import threading
import time

import pytest
from screen_clip import CUTS, PLANTED, PLANTED_AT, SCENE_SECONDS, SCENES, mono_font, screen_clip

from split_video import assets, ocr, scenes
from split_video.jobs import JobCancelledError
from split_video.local_media_store import LocalMediaStore

pytestmark = pytest.mark.skipif(
    shutil.which("tesseract") is None or shutil.which("ffmpeg") is None or mono_font() is None,
    reason="needs the tesseract program, ffmpeg and DejaVu Sans Mono",
)
RECALL = 0.8  # the bar from the decision note (SPL-96: 0.83 on harder, real-looking screens)


def tokens(text):
    return re.findall(r"[^\W_]+", text.casefold())


@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory):
    path = tmp_path_factory.mktemp("screen") / "screen.mp4"
    screen_clip(path)
    return path.read_bytes()


@pytest.fixture
def local(tmp_path, clip_bytes):
    (tmp_path / "screen.mp4").write_bytes(clip_bytes)
    media = LocalMediaStore(tmp_path.resolve())
    return media, assets.register_path(media, "screen.mp4"), tmp_path


def recall(document, scene):
    truth = [t for text, _, _ in SCENES[scene][1] for t in tokens(text)]
    start = scene * SCENE_SECONDS
    seen = {
        t
        for f in document["frames"]
        if f["changed"] and start <= f["time"] < start + SCENE_SECONDS
        for t in tokens(f["text"])
    }
    return sum(t in seen for t in truth) / len(truth), [t for t in truth if t not in seen]


def test_the_engine_reads_the_known_text_of_each_screen(local):
    media, asset, _ = local
    result = ocr.recognize(media, asset.id, interval=4.0)
    assert result["engine"].startswith("tesseract ") and result["made"] is True and result["frames"] >= 9
    assert result["ocr_frames"] == 3 and result["skipped_unchanged"] == result["frames"] - 3  # three screens
    document = ocr.load(media, asset.id)
    for scene, name in enumerate(("code on dark", "red error on black", "dark text on a light slide")):
        found, missing = recall(document, scene)
        assert found >= RECALL, f"{name}: recall {found:.2f}, missed {missing}"
    texts = " ".join(f["text"] for f in document["frames"])
    assert "FAILED" in texts and "AssertionError" in texts and "silence detection" in texts


def test_the_planted_identifier_is_found_with_a_time_within_the_sampling_interval(local):
    media, asset, _ = local
    interval = 5.0
    ocr.recognize(media, asset.id, interval=interval)
    found = ocr.ocr_search(media, asset.id, PLANTED)
    assert found["total_matches"] == 1
    hit = found["hits"][0]
    assert hit["source"] == "ocr" and hit["score"] >= 0.9 and PLANTED_AT[0] <= hit["time"] <= PLANTED_AT[0] + interval
    assert PLANTED_AT[1] <= hit["end"] <= PLANTED_AT[1] + interval  # it was replaced by the slide
    assert "FAILED" in hit["text"]
    # the identifier is also found from only part of it, and from its words as Tesseract may split them
    assert ocr.ocr_search(media, asset.id, "cut burst keeps strongest")["hits"][0]["time"] == hit["time"]
    assert ocr.ocr_search(media, asset.id, "video editor agents")["hits"][0]["time"] >= PLANTED_AT[1]
    assert ocr.ocr_search(media, asset.id, "kubernetes")["total_matches"] == 0


def test_with_scenes_the_scene_frame_lands_just_after_the_cut(local):
    media, asset, _ = local
    scenes.analyze(media, asset.id)
    cuts = [s["time"] for s in scenes.load(media, asset.id)["scenes"]]
    assert cuts  # the light slide at 24 s is a hard cut
    result = ocr.recognize(media, asset.id, interval=20.0)
    assert result["scenes_used"] is True and result["scene_frames"] == len(cuts) and result["note"] is None
    slide = ocr.ocr_search(media, asset.id, "silence detection")["hits"][0]
    assert abs(slide["time"] - (CUTS[1] + ocr.SCENE_OFFSET)) < 1.0


def test_dark_and_light_frames_take_their_own_preprocessing(local):
    media, asset, _ = local
    engine = ocr.default_engine()
    from split_video import frames

    dark = frames.extract_frame(media, asset.id, 3.0, width=1280, format="png", source="original").data
    light = frames.extract_frame(media, asset.id, 30.0, width=1280, format="png", source="original").data
    assert ocr.is_dark(engine.signature(dark, None)) and not ocr.is_dark(engine.signature(light, None))
    # Both orientations read the clean synthetic frame (the inversion mattered on the decision note's busier
    # screens, not on this one); what is tested is that each path through the preprocessing works.
    truth = [t for text, _, _ in SCENES[0][1] for t in tokens(text)]
    for flag in (True, False):
        found = set(tokens(engine.read(dark, time=3.0, dark=flag, cancel=None)))
        assert sum(t in found for t in truth) / len(truth) >= RECALL, flag
    assert "silence" in tokens(engine.read(light, time=30.0, dark=False, cancel=None))


class Token:
    """What a job's context offers: a flag, `raise_if_cancelled` and callbacks run on cancel."""

    def __init__(self):
        self.flag = False
        self.callbacks = []

    def cancel(self):
        self.flag = True
        for callback in list(self.callbacks):
            callback()

    def raise_if_cancelled(self):
        if self.flag:
            raise JobCancelledError("job cancelled")

    def on_cancel(self, callback):
        self.callbacks.append(callback)
        if self.flag:
            callback()
        return lambda: self.callbacks.remove(callback) if callback in self.callbacks else None


def test_a_real_cancel_stops_between_frames_and_kills_the_running_processes(local):
    media, asset, _ = local
    token = Token()
    seen = []
    outcome = {}

    def work():
        try:
            ocr.recognize(
                media, asset.id, interval=1.0, force=True, cancel=token, on_progress=lambda d, t: seen.append(d)
            )
        except JobCancelledError:
            outcome["cancelled"] = time.time()

    thread = threading.Thread(target=work)
    thread.start()
    deadline = time.time() + 30
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.4)
    asked = time.time()
    token.cancel()
    thread.join(20)
    assert not thread.is_alive() and "cancelled" in outcome and outcome["cancelled"] - asked < 8
    assert ocr.load(media, asset.id) is None and not ocr.has_ocr(media, asset)
    import subprocess

    assert (
        subprocess.run(["pgrep", "-f", "prepared.png"], capture_output=True, check=False).returncode == 1
    )  # nothing left running
