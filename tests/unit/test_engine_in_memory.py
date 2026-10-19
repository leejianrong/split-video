"""The engine runs against in-memory stores with nothing on disk.

Projects live in an `InMemoryProjectStore`, media (sources, outputs, derived
caches) in an `InMemoryMediaStore`; only the ffmpeg calls themselves are
replaced with fakes.
"""

import json
import struct

import numpy as np
import pytest

from split_video import project, render, timeline_service
from split_video import timeline_ops as ops
from split_video.editor import cache as cache_module
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache, SilenceCache, WaveformCache
from split_video.ffmpeg import MediaInfo
from split_video.media_store import InMemoryMediaStore
from split_video.silence import SilenceInterval
from split_video.store import InMemoryProjectStore, ProjectStore, SavedSegment
from split_video.timeline import (
    OutputExistsError,
    OutsideRootError,
    ProjectExistsError,
    ProjectNotFoundError,
    SourceNotFoundError,
    TimelineError,
    empty_project,
)

VIDEO = MediaInfo(duration=30.0, width=320, height=240, has_audio=True)


@pytest.fixture
def media():
    store = InMemoryMediaStore()
    store.add("gig.mp4", b"gig-bytes", info=MediaInfo(100.0, 320, 240, True))
    store.add("a.mp4", b"a-bytes", info=VIDEO)
    store.add("sub/b.mp4", b"b-bytes", info=VIDEO)
    return store


def test_in_memory_store_satisfies_the_interface():
    assert isinstance(InMemoryProjectStore(), ProjectStore)


def test_split_project_edits_through_the_stores(media):
    store = InMemoryProjectStore()

    assert not project.has_project(store, "gig.mp4")
    assert project.current_segments(store, media, "gig.mp4") == [SavedSegment(start=0.0, end=100.0)]

    segments, split = project.add_split(project.current_segments(store, media, "gig.mp4"), 40.0)
    project.save_segments(store, "gig.mp4", segments)
    assert split == 1 and project.has_project(store, "gig.mp4")

    labelled = project.update_segment(project.current_segments(store, media, "gig.mp4"), 2, label=" Encore ")
    project.save_segments(store, "gig.mp4", labelled)
    saved = store.load_split("gig.mp4")
    assert saved is not None and [(s.start, s.end, s.label) for s in saved] == [
        (0.0, 40.0, ""),
        (40.0, 100.0, "Encore"),
    ]

    moved, applied = project.move_split(project.current_segments(store, media, "gig.mp4"), 1, 55.0)
    project.save_segments(store, "gig.mp4", moved)
    assert applied == 55.0
    merged = project.remove_split(project.current_segments(store, media, "gig.mp4"), 1)
    project.save_segments(store, "gig.mp4", merged)
    assert len(project.current_segments(store, media, "gig.mp4")) == 1
    assert store.load_split("other.mp4") is None


def test_export_plans_and_writes_through_the_media_store(media, monkeypatch):
    written_by_ffmpeg = []

    def fake_extract(source, start, end, out_path, precise, output_format=None, cancel=None):
        written_by_ffmpeg.append((source.name, start, end, out_path.name))

    monkeypatch.setattr(project, "extract_segment", fake_extract)
    plan = project.plan_export(media, "gig.mp4", [(0, 40, None), (40, 100, "Encore")])
    assert plan.output_dir == "gig_split" and plan.filenames == ["01 - gig.mp4", "Encore.mp4"]

    started, done = [], []
    files, manifest = project.run_export(
        media, plan, parameters={"precise": False}, on_file_start=started.append, on_file_done=done.append
    )
    assert files == done == ["gig_split/01 - gig.mp4", "gig_split/Encore.mp4"]
    assert started == plan.filenames and manifest == "gig_split/manifest.json"
    assert written_by_ffmpeg == [("gig.mp4", 0, 40, "01 - gig.mp4"), ("gig.mp4", 40, 100, "Encore.mp4")]
    assert all(media.exists(f) for f in files)  # committed to the store
    data = json.loads(media.read(manifest))
    assert data["source_file"] == "gig.mp4" and [s["file"] for s in data["segments"]] == plan.filenames

    with pytest.raises(project.ExportConflictError):
        project.plan_export(media, "gig.mp4", [(0, 40, None), (40, 100, "Encore")])
    project.plan_export(media, "gig.mp4", [(0, 40, None), (40, 100, "Encore")], overwrite=True)


def test_propose_segments_uses_the_silence_cache(media, monkeypatch):
    scans = []

    def fake_detect(path, threshold, min_duration):
        scans.append(threshold)
        return [SilenceInterval(start=40.0, end=45.0)]

    monkeypatch.setattr(cache_module, "detect_silence", fake_detect)
    first = project.propose_segments(media, "gig.mp4", min_song_length=10.0)
    second = project.propose_segments(media, "gig.mp4", min_song_length=10.0)
    assert first == second and len(first) == 2
    assert scans == ["-35.0dB"]  # the second proposal was a cache hit


def test_silence_cache_hits_survive_a_new_instance_and_miss_after_the_media_changes(media, monkeypatch):
    calls = []

    def fake_detect(path, threshold, min_duration):
        calls.append(threshold)
        return [SilenceInterval(start=1.0, end=2.0)]

    monkeypatch.setattr(cache_module, "detect_silence", fake_detect)
    assert SilenceCache(media, "a.mp4").get_raw_silences(-35.0) == [SilenceInterval(start=1.0, end=2.0)]
    assert SilenceCache(media, "a.mp4").get_raw_silences(-35.0) == [SilenceInterval(start=1.0, end=2.0)]
    assert len(calls) == 1
    media.add("a.mp4", b"a-bytes", info=VIDEO, mtime=2.0)  # same size, new mtime
    SilenceCache(media, "a.mp4").get_raw_silences(-35.0)
    assert len(calls) == 2


def test_waveform_cache_hit_and_miss(media, monkeypatch):
    pcm = struct.pack("<4h", 32767, 0, -32768, 0)
    calls = []
    monkeypatch.setattr(
        cache_module,
        "iter_pcm_chunks",
        lambda *a, **k: calls.append(1) or (c for c in [np.frombuffer(pcm, dtype="<i2")]),
    )
    peaks = WaveformCache(media, "a.mp4").get_peaks()
    assert WaveformCache(media, "a.mp4").get_peaks() == peaks and len(calls) == 1
    media.add("a.mp4", b"different, longer", info=VIDEO)
    WaveformCache(media, "a.mp4").get_peaks()
    assert len(calls) == 2


def test_classification_cache_and_analysis_through_the_media_store(media, monkeypatch):
    scores = [{b: 0.1 for b in classify.DEFAULT_THRESHOLDS} | {"music": 0.9}]
    regions = classify.rethreshold(scores, classify.DEFAULT_THRESHOLDS, 0.48)
    monkeypatch.setattr(classify, "get_model_and_labels", lambda: (object(), object()))
    monkeypatch.setattr(classify, "classify_stream", lambda *a, **k: (regions, scores, 0.48))
    monkeypatch.setattr(project, "iter_pcm_chunks", lambda *a, **k: (c for c in []))

    cache = ClassificationCache(media, "a.mp4")
    assert not cache.is_analyzed
    project.run_analysis(media, "a.mp4", cache, cache.get_thresholds())
    reloaded = ClassificationCache(media, "a.mp4")
    assert reloaded.is_analyzed and reloaded.get_regions() == regions
    assert project.summarize_classification(reloaded)["analyzed"] is True


def test_timeline_edits_through_the_stores(media):
    store = InMemoryProjectStore()

    with pytest.raises(ProjectNotFoundError):
        timeline_service.load_existing(store, "show")
    created = timeline_service.create_project(store, "show")
    assert store.list_timelines() == ["show"]
    with pytest.raises(ProjectExistsError):
        timeline_service.create_project(store, "show")
    with pytest.raises(TimelineError):
        timeline_service.create_project(store, "../evil")

    after_clip = timeline_service.add_clip(store, media, "show", "a.mp4", in_point=2.0, out_point=12.0)
    clip = after_clip.tracks[0].clips[0]
    assert clip.source == "a.mp4" and clip.duration == 10.0
    assert timeline_service.load_existing(store, "show") == after_clip != created
    assert timeline_service.source_duration(media, "a.mp4") == 30.0
    assert timeline_service.source_duration(media, "missing.mp4") is None
    assert timeline_service.source_duration(media, "../escape.mp4") is None

    timeline_service.add_clip(store, media, "show", "/sub/b.mp4")  # a leading slash is root-relative
    assert timeline_service.load_existing(store, "show").tracks[0].clips[1].source == "sub/b.mp4"
    timeline_service.remove_clip(store, "show", timeline_service.load_existing(store, "show").tracks[0].clips[1].id)

    timeline_service.add_track(store, "show", "video")
    top = timeline_service.load_existing(store, "show").tracks[1]
    timeline_service.add_text_clip(store, "show", top.id, "Hello", 3.0)
    timeline_service.set_clip_properties(store, "show", clip.id, gain=0.5)
    timeline_service.set_transition(store, "show", clip.id, "cut")
    trimmed = timeline_service.trim_clip(store, media, "show", clip.id, out_point=8.0)
    assert trimmed.tracks[0].clips[0].gain == 0.5 and trimmed.tracks[0].clips[0].duration == 6.0
    assert trimmed.tracks[1].clips[0].text == "Hello"
    described = timeline_service.describe(media, "show", trimmed)
    assert described["tracks"][0]["clips"][0]["source_duration"] == 30.0

    project_, filename, out_id = timeline_service.plan_render(store, media, "show", None, False)
    assert filename == out_id == "show-render.mp4" and project_ == trimmed
    media.write_bytes("show-render.mp4", b"old")
    with pytest.raises(OutputExistsError):
        timeline_service.plan_render(store, media, "show", None, False)
    timeline_service.plan_render(store, media, "show", None, True)

    timeline_service.remove_clip(store, "show", clip.id)
    assert not timeline_service.load_existing(store, "show").tracks[0].clips


def test_adding_a_clip_checks_the_media_store(media):
    store = InMemoryProjectStore()
    timeline_service.create_project(store, "p")
    with pytest.raises(OutsideRootError):
        timeline_service.add_clip(store, media, "p", "../x.mp4")
    with pytest.raises(SourceNotFoundError):
        timeline_service.add_clip(store, media, "p", "nope.mp4")
    media.add("silent.mp3", b"x", info=MediaInfo(5.0, None, None, True))
    with pytest.raises(TimelineError, match="no video stream"):
        timeline_service.add_clip(store, media, "p", "silent.mp3")


def test_render_reads_every_source_and_writes_the_output_through_the_media_store(media, monkeypatch):
    p = empty_project()
    p, _ = ops.add_clip(p, p.tracks[0].id, "a.mp4", 30.0, out_point=5.0)
    p, _ = ops.add_clip(p, p.tracks[0].id, "sub/b.mp4", 30.0, out_point=4.0)
    captured = {}

    def fake_ffmpeg(plan, inputs, out_path, total, progress, cancel=None):
        captured.update(inputs=inputs, out=out_path, total=total)
        progress(1.0)

    monkeypatch.setattr(render, "_run_ffmpeg", fake_ffmpeg)
    seen = []
    render.render(p, media, "out.mp4", seen.append)
    assert [i.split("/")[-1] for i in captured["inputs"]] == ["a.mp4", "b.mp4"]
    assert captured["out"].endswith("out.mp4") and captured["total"] == 9.0 and seen == [1.0]
    assert media.exists("out.mp4")

    escaping = empty_project()
    escaping, _ = ops.add_clip(escaping, escaping.tracks[0].id, "../x.mp4", 10.0)
    with pytest.raises(OutsideRootError):
        render.render(escaping, media, "o.mp4")
    missing = empty_project()
    missing, _ = ops.add_clip(missing, missing.tracks[0].id, "gone.mp4", 10.0)
    with pytest.raises(TimelineError, match="not found"):
        render.render(missing, media, "o.mp4")


def test_a_failed_edit_leaves_the_stored_project_unchanged():
    store = InMemoryProjectStore()
    before = timeline_service.create_project(store, "p")
    with pytest.raises(TimelineError):
        timeline_service.edit(store, "p", lambda p: (_ for _ in ()).throw(TimelineError("nope")))
    assert store.load_timeline("p") == before
