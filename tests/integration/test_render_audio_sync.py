"""Rendering mixes of audio-bearing (A) and silent (S) clips: picture and sound stay in sync.

Every combination renders for real at 160x90 with ~1.5 s clips. A clips carry a 440 Hz tone, S clips
have no audio stream at all. Today's behaviour, kept on purpose: the output always has an audio
stream, and it is silence wherever no clip makes sound (even when no clip has audio).
"""

import itertools
import json
import subprocess

import pytest

from split_video import timeline_ops as ops
from split_video.ffmpeg import probe_duration
from split_video.local_media_store import LocalMediaStore
from split_video.render import render
from split_video.timeline import empty_project

CLIP = 1.5
FADE = 0.4
AUDIBLE_DB = -35.0  # a 440 Hz sine at ffmpeg's default amplitude is about -21 dB; silence is far below
SILENT_DB = -60.0


def _run(cmd):
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0, cmd


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("sync")
    video = ["-f", "lavfi", "-i", f"color=c=red:size=160x90:rate=25:duration={CLIP}"]
    enc = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    tone = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={CLIP}", "-c:a", "aac"]
    _run(["ffmpeg", "-y", *video, *tone, *enc, str(root / "a.mp4")])
    _run(["ffmpeg", "-y", *video, *enc, str(root / "s.mp4")])
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=880:duration={CLIP}", str(root / "music.wav")])
    return root


def _window_db(path, start, length):
    """Peak level in dB of `length` seconds of the output's audio from `start` (-100 for digital silence)."""
    err = subprocess.run(
        ["ffmpeg", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    ).stderr  # fmt: skip
    line = next((ln for ln in err.splitlines() if "max_volume:" in ln), None)
    return float(line.split("max_volume:")[1].split("dB")[0]) if line else -100.0


def _stream_durations(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return {s["codec_type"]: float(s["duration"]) for s in json.loads(out)["streams"]}


def _build(seq, fades):
    """A main-track project from a string like "ASA" and per-join fades (None = cut). Returns the project
    and each clip's (start, end, fade_in, fade_out) in output time."""
    p = empty_project()
    tid = p.tracks[0].id
    spans, t = [], 0.0
    for i, ch in enumerate(seq):
        p, clip = ops.add_clip(p, tid, "a.mp4" if ch == "A" else "s.mp4", CLIP)
        fade_in = fades[i - 1] if i else None
        if fade_in:
            p = ops.set_transition(p, clip.id, "crossfade", fade_in)
            t -= fade_in
        spans.append([t, t + CLIP, fade_in or 0.0, 0.0])
        t += CLIP
    for i in range(len(seq) - 1):
        spans[i][3] = spans[i + 1][2]
    return p, spans


def _check_main(out, seq, spans):
    expected = spans[-1][1]
    assert probe_duration(out) == pytest.approx(expected, abs=0.1)
    streams = _stream_durations(out)
    assert streams["audio"] == pytest.approx(streams["video"], abs=0.1)
    assert streams["audio"] == pytest.approx(expected, abs=0.1)
    for ch, (start, end, fade_in, fade_out) in zip(seq, spans):
        lo, hi = start + fade_in + 0.1, end - fade_out - 0.1  # inside the clip, clear of any crossfade
        db = _window_db(out, lo, hi - lo)
        if ch == "A":
            assert db > AUDIBLE_DB, f"clip at {start:.2f}s should be audible, got {db} dB"
        else:
            assert db < SILENT_DB, f"clip at {start:.2f}s should be silent, got {db} dB"


def _sequences():
    for n in (2, 3):
        yield from ("".join(s) for s in itertools.product("AS", repeat=n))
    yield from ("ASAS", "SASA", "AASS", "SSAA", "ASSA")


def _fade_patterns(n):
    """Every cut/crossfade pattern for the n-1 joins of an n-clip sequence."""
    return itertools.product((None, FADE), repeat=n - 1)


CASES = [(seq, fades) for seq in _sequences() for fades in _fade_patterns(len(seq))]


def _case_id(case):
    seq, fades = case
    return seq + "-" + "".join("x" if f else "c" for f in fades)


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_main_track_audio_stays_aligned(media, case):
    seq, fades = case
    p, spans = _build(seq, fades)
    out = media / "out.mp4"
    render(p, LocalMediaStore(media), out.name)
    _check_main(out, seq, spans)


def test_inserting_a_clip_before_a_cut_then_crossfade_renders(media):
    """SPL-109: add_clip(index=0) of an audio clip ahead of silent clips, the second crossfaded in."""
    p = empty_project()
    tid = p.tracks[0].id
    p, _ = ops.add_clip(p, tid, "s.mp4", CLIP)
    p, s2 = ops.add_clip(p, tid, "s.mp4", CLIP)
    p = ops.set_transition(p, s2.id, "crossfade", FADE)
    p, _ = ops.add_clip(p, tid, "a.mp4", CLIP, index=0)
    out = media / "out.mp4"
    render(p, LocalMediaStore(media), out.name)
    assert probe_duration(out) == pytest.approx(3 * CLIP - FADE, abs=0.1)
    assert _window_db(out, 0.1, 1.2) > AUDIBLE_DB
    assert _window_db(out, 1.7, 1.2) < SILENT_DB


def test_no_audio_anywhere_still_has_a_silent_audio_stream(media):
    p, spans = _build("SS", (FADE,))
    out = media / "out.mp4"
    render(p, LocalMediaStore(media), out.name)
    assert "audio" in _stream_durations(out)
    assert _window_db(out, 0.0, spans[-1][1]) < SILENT_DB


@pytest.mark.parametrize("main", ["SS", "AA", "AS"])
@pytest.mark.parametrize("overlay_source", ["a.mp4", "s.mp4"])
@pytest.mark.parametrize("fades", [(None,), (FADE,)])
def test_overlay_clip_sound_lands_in_its_own_window(media, main, overlay_source, fades):
    p, spans = _build(main, fades)
    p, track = ops.add_track(p, "video")
    start = 0.4
    p, ov = ops.add_clip(p, track.id, overlay_source, CLIP, start=start)
    p = ops.set_clip_properties(p, ov.id, scale=0.5)
    out = media / "out.mp4"
    render(p, LocalMediaStore(media), out.name)
    total = max(spans[-1][1], start + CLIP)
    assert probe_duration(out) == pytest.approx(total, abs=0.1)
    streams = _stream_durations(out)
    assert streams["audio"] == pytest.approx(streams["video"], abs=0.1)
    # Past the main track the only sound is the overlay's, so that tail shows where it landed.
    tail_from = spans[-1][1] + 0.1
    if total - tail_from > 0.1:
        db = _window_db(out, tail_from, total - tail_from - 0.05)
        assert (db > AUDIBLE_DB) == (overlay_source == "a.mp4")


@pytest.mark.parametrize("main", ["S", "A", "SA"])
@pytest.mark.parametrize("fades", [(None,), (FADE,)])
def test_audio_track_clip_on_top_is_placed_in_time(media, main, fades):
    p, spans = _build(main, fades[: len(main) - 1])
    p, track = ops.add_track(p, "audio")
    start = spans[-1][1] + 0.3  # after the main track, so nothing else makes sound there
    p, _ = ops.add_clip(p, track.id, "music.wav", CLIP, start=start)
    out = media / "out.mp4"
    render(p, LocalMediaStore(media), out.name)
    assert probe_duration(out) == pytest.approx(start + CLIP, abs=0.1)
    streams = _stream_durations(out)
    assert streams["audio"] == pytest.approx(streams["video"], abs=0.1)
    assert _window_db(out, 0.0, spans[-1][1]) > AUDIBLE_DB if "A" in main else True
    assert _window_db(out, spans[-1][1] + 0.05, 0.2) < SILENT_DB  # the gap before the music
    assert _window_db(out, start + 0.1, CLIP - 0.2) > AUDIBLE_DB


def test_text_clip_over_silent_main_keeps_audio_in_sync(media):
    p, spans = _build("SA", (FADE,))
    p, track = ops.add_track(p, "video")
    p, _ = ops.add_text_clip(p, track.id, "hello", 1.0, start=0.2)
    out = media / "out.mp4"
    render(p, LocalMediaStore(media), out.name)
    _check_main(out, "SA", spans)
