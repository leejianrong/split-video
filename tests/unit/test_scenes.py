"""Scene changes and the activity curve: parsing, storage, currency, progress and cancel with fake ffmpeg output on
the in-memory store, then the real pass on a synthetic clip with cuts at known times (SPL-43)."""

import subprocess

import pytest

from split_video import assets, proxy, scenes
from split_video.ffmpeg import ExtractError, MediaInfo
from split_video.jobs import JobCancelledError
from split_video.local_media_store import LocalMediaStore
from split_video.media_store import InMemoryMediaStore

CUTS = [5.0, 12.0, 20.0]
TOLERANCE = 0.5


def frame_lines(time, mafd, score, changed=0.0):
    """What `metadata=print` writes for one frame: scdet's numbers, then signalstats' (the YAVG of the 0/255 mask of
    changed pixels, so `changed` is the fraction of the picture)."""
    return [
        f"frame:{round(time * 5)}   pts:{round(time * 5)}      pts_time:{time:g}\n",
        f"lavfi.scd.mafd={mafd:.3f}\n",
        f"lavfi.scd.score={score:.3f}\n",
        "lavfi.signalstats.YMIN=0\n",
        f"lavfi.signalstats.YAVG={changed * 255:.3f}\n",
        "lavfi.signalstats.YMAX=255\n",
    ]


def fake_run(frames):
    """A stand-in for `iter_stdout_lines` yielding the given `(time, mafd, score[, changed])` frames."""

    def run(cmd, cancel=None, what="ffmpeg"):
        run.commands.append(cmd)
        for f in frames:
            yield from frame_lines(*f)

    run.commands = []
    return run


# 10 s at 5 fps: still, a cut at 4.0 (score 40), typing-level changes at 6-7, a weaker cut at 8.0
FRAMES = [(k / 5, 0.0, 0.0) for k in range(50)]
FRAMES[20] = (4.0, 40.0, 40.0)
FRAMES[30] = (6.0, 0.3, 0.3)
FRAMES[35] = (7.0, 0.3, 0.3)
FRAMES[40] = (8.0, 20.0, 18.0)
FRAMES[41] = (8.2, 20.0, 1.0)  # the burst after a cut: still the same cut under min_gap


@pytest.fixture
def memory():
    media = InMemoryMediaStore()
    media.add("show/clip.mp4", b"video", info=MediaInfo(10.0, 640, 360, True, 25.0))
    return media, assets.register_path(media, "show/clip.mp4")


# --- parsing --------------------------------------------------------------------------------------


def test_activity_curve_is_logarithmic_zero_for_still_and_one_for_a_full_change():
    assert scenes.activity_of(0.0) == 0.0
    assert 0.0 < scenes.activity_of(0.1) < scenes.activity_of(0.3) < scenes.activity_of(5.0) < 1.0
    assert scenes.activity_of(scenes.FULL_ACTIVITY_MAFD) == 1.0 and scenes.activity_of(200.0) == 1.0


def test_parser_finds_cuts_buckets_the_activity_and_merges_a_burst():
    parser = scenes.SignalParser(threshold=0.15, min_gap=1.0)
    for f in FRAMES:
        for line in frame_lines(*f):
            parser.feed(line)
    found, activity = parser.result(10.0)
    assert [(s["time"], s["score"], s["kind"]) for s in found] == [(4.0, 0.4, "cut"), (8.0, 0.18, "cut")]
    assert len(activity) == 10
    assert activity[0] == 0.0 and activity[3] == 0.0
    assert activity[4] > 0.4 and activity[5] == 0.0  # the cut's bucket is the spike
    assert 0.0 < activity[6] < 0.2 and 0.0 < activity[7] < 0.2  # typing: low but not zero
    assert activity[8] > 0.4


def test_the_strongest_cut_of_a_burst_stands_for_it_and_a_gap_of_zero_keeps_all():
    burst = [(5.0, 10.0, 20.0), (5.2, 10.0, 30.0), (5.4, 10.0, 25.0)]
    for gap, expected in ((1.0, [5.2]), (0.0, [5.0, 5.2, 5.4])):
        parser = scenes.SignalParser(threshold=0.1, min_gap=gap)
        for f in burst:
            for line in frame_lines(*f):
                parser.feed(line)
        assert [s["time"] for s in parser.result(6.0)[0]] == expected


def test_the_threshold_decides_what_is_a_cut():
    def cuts(threshold):
        parser = scenes.SignalParser(threshold, 1.0)
        for f in FRAMES:
            for line in frame_lines(*f):
                parser.feed(line)
        return [s["time"] for s in parser.result(10.0)[0]]

    assert cuts(0.5) == [] and cuts(0.3) == [4.0] and cuts(0.002) == [4.0, 6.0, 7.0, 8.0]


def test_parser_ignores_noise_and_nan():
    parser = scenes.SignalParser(0.15, 1.0)
    for line in ["", "garbage\n", "frame:0 pts:0 pts_time:0\n", "lavfi.scd.mafd=nan\n", "lavfi.scd.score=nan\n"]:
        parser.feed(line)
    assert parser.result(2.0) == ([], [0.0, 0.0])


def test_between_helpers_cut_the_document():
    doc = {
        "scenes": [{"time": 5.0, "score": 0.6}, {"time": 12.0, "score": 0.4}, {"time": 20.0, "score": 0.3}],
        "activity": [round(k / 100, 2) for k in range(26)],
        "activity_interval": 1.0,
    }
    assert scenes.scenes_between(doc, 5.0, 20.0) == doc["scenes"][:2]  # [start, end)
    assert scenes.scenes_between(doc, None, 6.0) == doc["scenes"][:1] and len(scenes.scenes_between(doc)) == 3
    assert scenes.activity_between(doc, 2.5, 4.2) == [0.02, 0.03, 0.04]
    assert scenes.activity_between(doc, 24.0, None) == [0.24, 0.25]
    cut = scenes.excerpt(doc, 12.0, 14.0)
    assert cut["scenes"] == [doc["scenes"][1]] and cut["activity"] == [0.12, 0.13] and cut["activity_start"] == 12.0
    assert scenes.excerpt(doc) is doc


# --- analyze on the in-memory store -------------------------------------------------------------


def test_analyze_stores_the_document_and_reuses_it(memory, monkeypatch):
    media, asset = memory
    run = fake_run(FRAMES)
    monkeypatch.setattr(scenes, "iter_stdout_lines", run)
    first = scenes.analyze(media, asset.id)
    assert first == {
        "version": 2,
        "asset": asset.id,
        "source": "original",
        "duration": 10.0,
        "threshold": 0.15,
        "min_gap": 1.0,
        "screen_threshold": 0.02,
        "scenes": 2,
        "activity_buckets": 10,
        "made": True,
    }
    doc = scenes.load(media, asset.id)
    assert doc["source"] == "original" and doc["activity_interval"] == 1.0 and doc["sample_fps"] == 5.0
    assert [s["time"] for s in doc["scenes"]] == [4.0, 8.0] and len(doc["activity"]) == 10
    assert assets.get_asset(media, asset.id) and scenes.has_scenes(media, asset)
    vf = run.commands[0][run.commands[0].index("-vf") + 1]
    assert vf.startswith("fps=5,scale=160:-2") and "scdet" in vf and "metadata=mode=print" in vf
    assert "tblend=all_mode=difference" in vf and "signalstats" in vf and f"gt(val,{scenes.SCREEN_DELTA})" in vf

    again = scenes.analyze(media, asset.id)
    assert again["made"] is False and len(run.commands) == 1  # current: no second pass
    assert scenes.analyze(media, asset.id, force=True)["made"] is True and len(run.commands) == 2
    assert scenes.analyze(media, asset.id, threshold=0.3)["scenes"] == 1  # other parameters redo it
    assert scenes.load(media, asset.id)["threshold"] == 0.3
    assert scenes.analyze(media, asset.id, threshold=0.3, min_gap=3.0)["made"] is True
    assert scenes.analyze(media, asset.id, threshold=0.3, min_gap=3.0, screen_threshold=0.1)["made"] is True
    assert scenes.load(media, asset.id)["screen_threshold"] == 0.1


def test_a_slow_source_is_sampled_at_its_own_rate(monkeypatch):
    media = InMemoryMediaStore()
    media.add("slides.mp4", b"v", info=MediaInfo(10.0, 640, 360, False, 1.0))
    asset = assets.register_path(media, "slides.mp4")
    run = fake_run([])
    monkeypatch.setattr(scenes, "iter_stdout_lines", run)
    scenes.analyze(media, asset.id)
    assert "fps=1," in run.commands[0][run.commands[0].index("-vf") + 1]
    assert scenes.load(media, asset.id)["activity"] == [0.0] * 10  # no frames: no activity, full length


def test_a_changed_file_or_version_is_stale(memory, monkeypatch):
    media, asset = memory
    monkeypatch.setattr(scenes, "iter_stdout_lines", fake_run(FRAMES))
    scenes.analyze(media, asset.id)
    stored = media.load_cache(asset.path, scenes.CACHE_NAME)
    media.save_cache(asset.path, scenes.CACHE_NAME, {**stored, "version": 0})
    assert scenes.load(media, asset.id) is None
    media.save_cache(asset.path, scenes.CACHE_NAME, {**stored, "asset": "a_other"})
    assert scenes.load(media, asset.id) is None
    media.save_cache(asset.path, scenes.INFO_NAME, {"version": 0, "asset": asset.id})
    assert not scenes.has_scenes(media, asset)
    assert scenes.analyze(media, asset.id)["made"] is True  # a stale info entry is redone


def test_progress_counts_seconds_and_ends_at_the_total(memory, monkeypatch):
    media, asset = memory
    monkeypatch.setattr(scenes, "iter_stdout_lines", fake_run(FRAMES))
    seen = []
    scenes.analyze(media, asset.id, on_progress=lambda done, total: seen.append((done, total)))
    assert seen[0] == (0.0, 10.0) and seen[-1] == (10.0, 10.0)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen) and len(seen) > 5


class CancelAfter:
    def __init__(self, calls):
        self.calls = calls

    def raise_if_cancelled(self):
        self.calls -= 1
        if self.calls < 0:
            raise JobCancelledError("job cancelled")

    def on_cancel(self, callback):
        return lambda: None


def test_cancel_stops_the_pass_and_stores_nothing(memory, monkeypatch):
    media, asset = memory
    monkeypatch.setattr(scenes, "iter_stdout_lines", fake_run(FRAMES))
    with pytest.raises(JobCancelledError):
        scenes.analyze(media, asset.id, cancel=CancelAfter(10))
    assert scenes.load(media, asset.id) is None and not scenes.has_scenes(media, asset)


def test_a_failed_pass_stores_nothing_and_keeps_the_old_signals(memory, monkeypatch):
    media, asset = memory
    monkeypatch.setattr(scenes, "iter_stdout_lines", fake_run(FRAMES))
    scenes.analyze(media, asset.id)

    def broken(cmd, cancel=None, what="ffmpeg"):
        yield from frame_lines(1.0, 50.0, 50.0)
        raise ExtractError("ffmpeg failed to analyse the scenes: boom")

    monkeypatch.setattr(scenes, "iter_stdout_lines", broken)
    with pytest.raises(ExtractError, match="boom"):
        scenes.analyze(media, asset.id, force=True)
    assert [s["time"] for s in scenes.load(media, asset.id)["scenes"]] == [4.0, 8.0]


def test_bad_parameters_and_audio_only_assets(memory, monkeypatch):
    media, asset = memory
    with pytest.raises(ValueError, match="threshold"):
        scenes.analyze(media, asset.id, threshold=1.5)
    with pytest.raises(ValueError, match="screen_threshold"):
        scenes.analyze(media, asset.id, screen_threshold=2)
    with pytest.raises(ValueError, match="min_gap"):
        scenes.analyze(media, asset.id, min_gap=-1)
    media.add("song.mp3", b"a", info=MediaInfo(3.0, None, None, True))
    song = assets.register_path(media, "song.mp3")
    monkeypatch.setattr(scenes, "iter_stdout_lines", lambda *a, **k: pytest.fail("ffmpeg must not run"))
    assert scenes.analyze(media, song.id) == {"asset": song.id, "skipped": "audio-only asset", "made": False}
    assert not scenes.has_scenes(media, song)


# --- the screen-change rule -----------------------------------------------------------------------------


def parse(frames, **kwargs):
    parser = scenes.SignalParser(**{"threshold": 0.15, "min_gap": 1.0, **kwargs})
    for f in frames:
        for line in frame_lines(*f):
            parser.feed(line)
    return parser.result(10.0)[0]


def calm(n=50, changed=0.0008):
    """Still frames with a blinking cursor's worth of change."""
    return [(k / 5, 0.02, 0.02, changed) for k in range(n)]


def test_an_isolated_burst_of_changed_pixels_is_a_screen_change_with_its_score_and_fraction():
    frames = calm()
    frames[25] = (5.0, 1.046, 1.046, 0.0572)  # a dark text pane swapped for another: scdet score 0.01
    found = parse(frames)
    assert found == [{"time": 5.0, "score": 0.01, "kind": "screen", "changed": 0.0572}]


def test_cursor_typing_and_scrolling_do_not_fire():
    typing = calm(changed=0.0015)
    typing[10] = (2.0, 0.04, 0.03, 0.0021)
    assert parse(typing) == []
    scroll = calm()
    for k in range(10, 30):  # continuous: high frame after frame, including its first and last frames
        scroll[k] = (k / 5, 5.0, 0.05, 0.26)
    assert parse(scroll) == []
    two = calm()
    two[10] = (2.0, 1.0, 1.0, 0.05)
    two[11] = (2.2, 1.0, 1.0, 0.05)  # a burst of two frames is not a one-frame change
    assert parse(two) == []


def test_a_burst_next_to_a_noisy_frame_is_not_isolated():
    frames = calm()
    frames[24] = (4.8, 0.2, 0.2, 0.012)  # above half the threshold: the screen was already changing
    frames[25] = (5.0, 1.0, 1.0, 0.05)
    assert parse(frames) == []


def test_a_burst_in_the_very_last_frame_stands_and_the_first_frame_cannot_be_one():
    frames = calm(10)
    frames[-1] = (1.8, 1.0, 1.0, 0.05)
    assert [s["time"] for s in parse(frames)] == [1.8]
    first = calm(10)
    first[0] = (0.0, 1.0, 1.0, 0.05)
    assert parse(first) == []


def test_the_screen_threshold_decides_and_zero_turns_the_rule_off():
    frames = calm()
    frames[25] = (5.0, 1.0, 1.0, 0.03)
    assert parse(frames, screen_threshold=0.02)[0]["kind"] == "screen"
    assert parse(frames, screen_threshold=0.05) == []
    # off: the frame ends at scdet's score line, and no signalstats lines are needed
    parser = scenes.SignalParser(0.15, 1.0, screen_threshold=0)
    for line in ["frame:1 pts:1 pts_time:0.2\n", "lavfi.scd.mafd=40\n", "lavfi.scd.score=40\n"]:
        parser.feed(line)
    assert parser.result(1.0)[0] == [{"time": 0.2, "score": 0.4, "kind": "cut"}]


def test_a_hard_cut_both_rules_see_counts_once_as_a_cut_and_the_screen_one_does_not_win_a_burst():
    frames = calm()
    frames[25] = (5.0, 60.0, 60.0, 0.99)  # scdet and the screen rule both see it
    found = parse(frames)
    assert found == [{"time": 5.0, "score": 0.6, "kind": "cut", "changed": 0.99}]
    mixed = calm()
    mixed[25] = (5.0, 1.0, 1.0, 0.05)  # a screen change ...
    mixed[27] = (5.4, 30.0, 30.0, 0.9)  # ... then a cut 0.4 s later: one detection, the cut
    mixed[28] = (5.6, 0.0, 0.0, 0.0)
    assert [(s["time"], s["kind"]) for s in parse(mixed)] == [(5.4, "cut")]
    far = calm()
    far[25] = (5.0, 1.0, 1.0, 0.05)
    far[35] = (7.0, 1.0, 1.0, 0.08)  # two screen changes more than min_gap apart stay two
    assert [(s["time"], s["kind"]) for s in parse(far)] == [(5.0, "screen"), (7.0, "screen")]
    close = calm()
    close[25] = (5.0, 1.0, 1.0, 0.05)
    close[29] = (5.8, 1.0, 1.0, 0.08)  # within min_gap: the larger fraction stands
    assert [(s["time"], s["changed"]) for s in parse(close)] == [(5.8, 0.08)]


def test_a_version_1_document_is_still_readable_but_not_current(memory, monkeypatch):
    media, asset = memory
    old = {
        "version": 1,
        "asset": asset.id,
        "source": "original",
        "duration": 10.0,
        "threshold": 0.15,
        "min_gap": 1.0,
        "sample_fps": 5.0,
        "activity_interval": 1.0,
        "scenes": [{"time": 4.0, "score": 0.4}],
        "activity": [0.0] * 10,
    }
    summary = {k: v for k, v in old.items() if k not in ("sample_fps", "activity_interval", "scenes", "activity")}
    media.save_cache(asset.path, scenes.CACHE_NAME, old)
    media.save_cache(asset.path, scenes.INFO_NAME, {**summary, "scenes": 1, "activity_buckets": 10})
    loaded = scenes.load(media, asset.id)
    assert loaded["version"] == 1 and loaded["scenes"] == [{"kind": "cut", "time": 4.0, "score": 0.4}]
    assert scenes.has_scenes(media, asset) and "screen_threshold" not in loaded
    assert scenes.excerpt(loaded, 3.0, 5.0)["scenes"][0]["kind"] == "cut"
    run = fake_run(FRAMES)
    monkeypatch.setattr(scenes, "iter_stdout_lines", run)
    assert scenes.analyze(media, asset.id)["made"] is True and len(run.commands) == 1  # redone as version 2
    assert scenes.load(media, asset.id)["version"] == 2


# --- the real pass on a clip with known cuts ----------------------------------------------------


def known_cuts_clip(path):
    """26 s at 320x240, 25 fps. 0-5 s a still editor with a cursor stepping along (typing), 5-12 s a light screen
    with a small box sliding across (continuous motion), 12-20 s still colour bars, 20-26 s black. Cuts at 5, 12, 20."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    cmd += ["-f", "lavfi", "-i", "color=c=0x1e1e2e:s=320x240:r=25:d=5"]
    cmd += ["-f", "lavfi", "-i", "color=c=white:s=8x14:r=25:d=5"]
    cmd += ["-f", "lavfi", "-i", "color=c=0xc8c8c8:s=320x240:r=25:d=7"]
    cmd += ["-f", "lavfi", "-i", "color=c=0x202020:s=24x24:r=25:d=7"]
    cmd += ["-f", "lavfi", "-i", "smptebars=s=320x240:r=25:d=8"]
    cmd += ["-f", "lavfi", "-i", "color=c=black:s=320x240:r=25:d=6"]
    graph = "[0:v][1:v]overlay=x='10+floor(t*3)*8':y=100[a];[2:v][3:v]overlay=x='t*40':y=100[b];"
    graph += "[a][b][4:v][5:v]concat=n=4:v=1:a=0,format=yuv420p[o]"
    cmd += ["-filter_complex", graph, "-map", "[o]", "-c:v", "libx264", "-g", "50", str(path)]
    assert subprocess.run(cmd, capture_output=True, check=False).returncode == 0


@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory):
    path = tmp_path_factory.mktemp("known") / "known.mp4"
    known_cuts_clip(path)
    return path.read_bytes()


@pytest.fixture
def local(tmp_path, clip_bytes):
    (tmp_path / "stream.mp4").write_bytes(clip_bytes)
    media = LocalMediaStore(tmp_path.resolve())
    return media, assets.register_path(media, "stream.mp4"), tmp_path


def mean(values):
    return sum(values) / len(values)


def test_the_planted_cuts_are_found_within_tolerance_and_activity_ranks(local):
    media, asset, _ = local
    result = scenes.analyze(media, asset.id)
    assert result["made"] and result["source"] == "original" and result["scenes"] == 3
    doc = scenes.load(media, asset.id)
    found = [s["time"] for s in doc["scenes"]]
    assert len(found) == len(CUTS) and all(abs(f - c) <= TOLERANCE for f, c in zip(found, CUTS, strict=True))
    assert all(0.15 <= s["score"] <= 1.0 for s in doc["scenes"])
    act = doc["activity"]
    assert len(act) == 26 and all(0.0 <= a <= 1.0 for a in act)
    spike = max(act[5], act[12], act[20])
    moving = mean(act[6:12])
    typing = mean(act[0:5])
    still = mean(act[13:20])
    assert spike > moving > typing > still == 0.0
    assert typing > 0.05 and spike > 0.5  # typing is low but not zero; a cut is a clear spike
    assert min(act[5], act[12], act[20]) > moving  # every planted cut beats the motion around it


def test_it_runs_on_the_proxy_when_there_is_one(local):
    media, asset, _ = local
    proxy.prepare(media, asset.id)
    result = scenes.analyze(media, asset.id)
    assert result["source"] == "proxy" and result["made"]
    doc = scenes.load(media, asset.id)
    assert doc["source"] == "proxy"
    assert all(abs(s["time"] - c) <= TOLERANCE for s, c in zip(doc["scenes"], CUTS, strict=True))
    assert len(doc["scenes"]) == 3
    act = doc["activity"]
    assert max(act[5], act[12], act[20]) > mean(act[6:12]) > mean(act[0:5]) > mean(act[13:20]) == 0.0


def test_without_a_proxy_it_falls_back_to_the_original_and_a_new_proxy_triggers_a_redo(local):
    media, asset, _ = local
    assert scenes.analyze(media, asset.id)["source"] == "original"
    assert scenes.analyze(media, asset.id)["made"] is False
    proxy.prepare(media, asset.id)
    redone = scenes.analyze(media, asset.id)
    assert redone["made"] is True and redone["source"] == "proxy"
    assert scenes.analyze(media, asset.id)["made"] is False


def test_a_stale_proxy_is_not_used(local):
    media, asset, root = local
    proxy.prepare(media, asset.id)
    (root / "stream.mp4").touch()  # a new mtime: the proxy and every stored signal are stale
    assert scenes.load(media, asset.id) is None
    stale = assets.get_asset(media, asset.id)
    assert proxy.proxy_info(media, stale) is None
    result = scenes.analyze(media, asset.id)
    assert result["source"] == "original" and result["scenes"] == 3


def test_a_lower_threshold_finds_more_and_min_gap_merges(local):
    media, asset, _ = local
    assert scenes.analyze(media, asset.id, threshold=0.9, screen_threshold=0)["scenes"] == 0
    # the screen rule is its own knob: the whole-picture changes below a strict scdet threshold still show, as "screen"
    assert scenes.analyze(media, asset.id, threshold=0.9)["scenes"] == 3
    wide = scenes.analyze(media, asset.id, min_gap=10.0)  # 5 and 12 are 7 s apart, 12 and 20 are 8 s
    assert wide["scenes"] == 2 and scenes.load(media, asset.id)["min_gap"] == 10.0


def test_a_real_cancel_terminates_ffmpeg_and_stores_nothing(local):
    media, asset, _ = local
    with pytest.raises(JobCancelledError):
        scenes.analyze(media, asset.id, cancel=CancelAfter(0))
    assert scenes.load(media, asset.id) is None


def test_the_listing_flags_assets_with_signals(local):
    from split_video import library

    media, asset, _ = local
    assert [a["scenes"] for a in library.listing(media)] == [False]
    scenes.analyze(media, asset.id)
    assert [a["scenes"] for a in library.listing(media)] == [True]
