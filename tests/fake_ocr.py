"""A fake OCR engine and fake frames for the on-screen text tests (SPL-98): no tesseract, no decoding.

`install_frames(monkeypatch)` replaces `frames.extract_frame` with one that returns the frame's time as its bytes;
`FakeEngine(screens)` "reads" those: `screens` is `[(start_second, text)]`, the screen at time t is the last entry
that started at or before t. Its signature is a dark plane with a number of bright pixels that grows with the screen's
index, so a new screen counts as changed and the same screen does not.
"""

from __future__ import annotations

import threading
import time as clock

from split_video import frames, ocr


def _screen_at(screens, time):
    index = 0
    for k, (start, _) in enumerate(screens):
        if start <= time:
            index = k
    return index


class FakeEngine:
    name = "fake-ocr 1.0"

    def __init__(self, screens, *, delay=0.0, on_read=None, fail_at=()):
        self.screens = screens
        self.delay = delay
        self.on_read = on_read
        self.fail_at = set(fail_at)
        self.reads: list[float] = []
        self.darks: list[bool] = []
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def signature(self, image, cancel):
        time = float(image.decode())
        bright = 200 * _screen_at(self.screens, time)  # each screen lights 200 more pixels than the one before
        size = ocr.SIGNATURE_WIDTH * ocr.SIGNATURE_HEIGHT
        return b"\xff" * bright + bytes([20]) * (size - bright)

    def read(self, image, *, time, dark, cancel):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.reads.append(time)
            self.darks.append(dark)
        try:
            if self.on_read is not None:
                self.on_read(time)
            if self.delay:
                clock.sleep(self.delay)
            return self.screens[_screen_at(self.screens, float(image.decode()))][1]
        finally:
            with self._lock:
                self.active -= 1


def install_frames(monkeypatch, *, fail_at=(), calls=None):
    """`frames.extract_frame` stand-in: the frame at T is the bytes `T` and is at T."""

    def extract(
        media, asset_id, time, *, width=None, height=None, format="jpeg", quality=85, source="auto", cancel=None
    ):
        if calls is not None:
            calls.append({"time": time, "width": width, "format": format, "source": source})
        if any(abs(time - t) < 1e-6 for t in fail_at):
            raise frames.FrameTimeError(f"no frame at {time}")
        return frames.Frame(
            data=f"{time:.3f}".encode(),
            mime="image/png",
            format="png",
            asset=asset_id,
            requested_time=time,
            time=round(time, 3),
            width=width or 0,
            height=0,
            source="original",
            duration=0.0,
        )

    monkeypatch.setattr(frames, "extract_frame", extract)


class ClipEngine:
    """For tests that decode real frames of `screen_clip`: ffmpeg makes the signature (real), the "reading" returns
    the known text of the scene the frame is in. Built lazily so the import needs no tesseract."""

    name = "fake-tesseract 5.0"

    def __new__(cls, *args, **kwargs):
        import shutil

        import screen_clip

        class Engine(ocr.TesseractEngine):
            def __init__(self, ffmpeg, delay=0.0, on_read=None):
                self.binary, self.ffmpeg, self.name = "fake", ffmpeg, "fake-tesseract 5.0"
                self.delay, self.on_read = delay, on_read

            def read(self, image, *, time, dark, cancel):
                if self.on_read is not None:
                    self.on_read(time)
                if self.delay:
                    clock.sleep(self.delay)
                scene = min(int(time // screen_clip.SCENE_SECONDS), len(screen_clip.SCENES) - 1)
                return "\n".join(text for text, _, _ in screen_clip.SCENES[scene][1])

        return Engine(shutil.which("ffmpeg"), *args, **kwargs)


def store_ocr(media, asset, screens, *, duration=11_000.0):
    """Store an on-screen text document for `asset` without running anything: `screens` is `[(time, text)]`, each
    on screen until the next one. Saved the way `ocr.recognize` saves it (document and counts entry)."""
    entries = []
    for k, (t, text) in enumerate(screens):
        until = screens[k + 1][0] if k + 1 < len(screens) else duration
        norm, lines = ocr.normalise(text)
        entries.append(
            {"time": t, "text": norm, "lines": lines, "changed": True, "source_scene": False, "until": until}
        )
    document = {
        "version": ocr.VERSION,
        "asset": asset.id,
        "engine": "fake-ocr 1.0",
        "duration": duration,
        "interval": 30.0,
        "max_frames": 2000,
        "scenes_used": False,
        "capped": False,
        "frames_before_cap": len(entries),
        "frames": entries,
    }
    summary = {k: v for k, v in document.items() if k != "frames"} | {"frames": len(entries)}
    media.save_cache(asset.path, ocr.CACHE_NAME, document)
    media.save_cache(asset.path, ocr.INFO_NAME, summary)
    return document
