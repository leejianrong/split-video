"""The engine runs against an in-memory ProjectStore with nothing on disk."""

import pytest

from split_video import project, timeline_service
from split_video.ffmpeg import MediaInfo
from split_video.store import InMemoryProjectStore, ProjectStore, SavedSegment
from split_video.timeline import ProjectExistsError, ProjectNotFoundError, TimelineError


def test_in_memory_store_satisfies_the_interface():
    assert isinstance(InMemoryProjectStore(), ProjectStore)


def test_split_project_edits_through_the_store(tmp_path, monkeypatch):
    monkeypatch.setattr(project, "probe_duration", lambda source: 100.0)
    store = InMemoryProjectStore()
    source = tmp_path / "gig.mp4"  # never created: the engine only probes it when nothing is saved

    assert not project.has_project(store, "gig.mp4")
    assert project.current_segments(store, "gig.mp4", source) == [SavedSegment(start=0.0, end=100.0)]

    segments, split = project.add_split(project.current_segments(store, "gig.mp4", source), 40.0)
    project.save_segments(store, "gig.mp4", segments)
    assert split == 1 and project.has_project(store, "gig.mp4")

    labelled = project.update_segment(project.current_segments(store, "gig.mp4", source), 2, label=" Encore ")
    project.save_segments(store, "gig.mp4", labelled)
    saved = store.load_split("gig.mp4")
    assert saved is not None and [(s.start, s.end, s.label) for s in saved] == [
        (0.0, 40.0, ""),
        (40.0, 100.0, "Encore"),
    ]

    moved, applied = project.move_split(project.current_segments(store, "gig.mp4", source), 1, 55.0)
    project.save_segments(store, "gig.mp4", moved)
    assert applied == 55.0
    merged = project.remove_split(project.current_segments(store, "gig.mp4", source), 1)
    project.save_segments(store, "gig.mp4", merged)
    assert len(project.current_segments(store, "gig.mp4", source)) == 1

    # Other ids are independent, and nothing was written next to the video.
    assert store.load_split("other.mp4") is None
    assert list(tmp_path.iterdir()) == []


def test_timeline_edits_through_the_store(tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_service, "probe_media", lambda path: MediaInfo(30.0, 320, 240, True))
    (tmp_path / "a.mp4").write_bytes(b"x")  # media stays on disk; only the project must not
    store = InMemoryProjectStore()

    with pytest.raises(ProjectNotFoundError):
        timeline_service.load_existing(store, "show")
    created = timeline_service.create_project(store, "show")
    assert store.list_timelines() == ["show"]
    with pytest.raises(ProjectExistsError):
        timeline_service.create_project(store, "show")
    with pytest.raises(TimelineError):
        timeline_service.create_project(store, "../evil")

    after_clip = timeline_service.add_clip(store, tmp_path, "show", "a.mp4", in_point=2.0, out_point=12.0)
    clip = after_clip.tracks[0].clips[0]
    assert clip.source == "a.mp4" and clip.duration == 10.0
    assert timeline_service.load_existing(store, "show") == after_clip != created

    timeline_service.add_track(store, "show", "video")
    top = timeline_service.load_existing(store, "show").tracks[1]
    timeline_service.add_text_clip(store, "show", top.id, "Hello", 3.0)
    timeline_service.set_clip_properties(store, "show", clip.id, gain=0.5)
    timeline_service.set_transition(store, "show", clip.id, "cut")
    trimmed = timeline_service.trim_clip(store, tmp_path, "show", clip.id, out_point=8.0)
    assert trimmed.tracks[0].clips[0].gain == 0.5 and trimmed.tracks[0].clips[0].duration == 6.0
    assert trimmed.tracks[1].clips[0].text == "Hello"

    project_, filename, out_path = timeline_service.plan_render(store, tmp_path, "show", None, False)
    assert filename == "show-render.mp4" and out_path == tmp_path / filename and project_ == trimmed

    timeline_service.remove_clip(store, "show", clip.id)
    assert not timeline_service.load_existing(store, "show").tracks[0].clips
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.mp4"]


def test_a_failed_edit_leaves_the_stored_project_unchanged():
    store = InMemoryProjectStore()
    before = timeline_service.create_project(store, "p")
    with pytest.raises(TimelineError):
        timeline_service.edit(store, "p", lambda p: (_ for _ in ()).throw(TimelineError("nope")))
    assert store.load_timeline("p") == before
