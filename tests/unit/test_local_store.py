"""LocalProjectStore: sidecar naming, formats and atomic writes."""

import json
import os

import pytest

from split_video import timeline
from split_video.local_store import LocalProjectStore
from split_video.store import SavedSegment
from split_video.timeline import Clip, Project, ProjectExistsError, ProjectNotFoundError, TimelineError, Track


def _project():
    clip = Clip(id="c1", source="a/one.mp4", in_point=1.0, out_point=6.0, gain=0.5, muted=True, x=0.2)
    clip2 = Clip(id="c2", source="two.mp4", in_point=0.0, out_point=4.0, start=5.0, transition="crossfade")
    return Project(tracks=(Track(id="t1", kind="video", clips=(clip, clip2)), Track(id="t2", kind="audio")))


# --- split projects ---------------------------------------------------------


def test_missing_split_project_is_a_miss(tmp_path):
    assert LocalProjectStore(tmp_path).load_split("video.mp4") is None


def test_split_round_trip_and_overwrite(tmp_path):
    store = LocalProjectStore(tmp_path)
    first = [SavedSegment(start=0.0, end=10.0), SavedSegment(start=10.0, end=25.5, label="x", color="#aabbcc")]
    store.save_split("video.mp4", first)
    assert store.load_split("video.mp4") == first
    second = [SavedSegment(start=0.0, end=8.0, included=False, export_name="n")]
    store.save_split("video.mp4", second)
    assert store.load_split("video.mp4") == second


def test_corrupt_split_project_is_a_miss(tmp_path):
    (tmp_path / "video.mp4.split-video-project.json").write_text("not json")
    assert LocalProjectStore(tmp_path).load_split("video.mp4") is None
    (tmp_path / "video.mp4.split-video-project.json").write_text('{"segments": [{"start": "x"}]}')
    assert LocalProjectStore(tmp_path).load_split("video.mp4") is None


def test_split_sidecar_sits_next_to_the_video_in_subdirectories(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.save_split("gigs/may/set.mp4", [SavedSegment(start=0.0, end=1.0)])
    store.save_split("other.mp4", [SavedSegment(start=0.0, end=2.0)])
    assert (tmp_path / "gigs/may/set.mp4.split-video-project.json").is_file()
    assert (tmp_path / "other.mp4.split-video-project.json").is_file()
    assert store.load_split("gigs/may/set.mp4") == [SavedSegment(start=0.0, end=1.0)]
    assert store.load_split("other.mp4") == [SavedSegment(start=0.0, end=2.0)]


def test_split_file_format_only_gained_a_revision_key(tmp_path):
    LocalProjectStore(tmp_path).save_split("gigs/set.mp4", [SavedSegment(start=0.0, end=1.5, label="A")])
    text = (tmp_path / "gigs/set.mp4.split-video-project.json").read_text()
    assert text == (
        '{"version":2,"revision":1,"source_file":"set.mp4","segments":[{"start":0.0,"end":1.5,"label":"A",'
        '"color":null,"included":true,"export_name":null}]}'
    )


@pytest.mark.parametrize("bad", ["", "/", "..", "../x.mp4", "a/../../x.mp4"])
def test_split_ids_cannot_escape_the_root(tmp_path, bad):
    store = LocalProjectStore(tmp_path)
    assert store.load_split(bad) is None
    with pytest.raises(ValueError):
        store.save_split(bad, [SavedSegment(start=0.0, end=1.0)])


def test_atomic_save_goes_through_a_temp_file_and_leaves_none_behind(tmp_path, monkeypatch):
    store = LocalProjectStore(tmp_path)
    replaced = []
    real = os.replace

    def spy(src, dst):
        assert str(src).endswith(".tmp") and os.path.exists(src)
        replaced.append(dst)
        real(src, dst)

    monkeypatch.setattr("split_video.local_store.os.replace", spy)
    store.save_split("v.mp4", [SavedSegment(start=0.0, end=1.0)])
    store.save_timeline("show", _project())
    assert len(replaced) == 2
    # No temp files left behind; each project file has only its (empty) lock file next to it.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "show.split-video-timeline.json",
        "show.split-video-timeline.json.lock",
        "v.mp4.split-video-project.json",
        "v.mp4.split-video-project.json.lock",
    ]


def test_a_failed_write_keeps_the_previous_file(tmp_path, monkeypatch):
    store = LocalProjectStore(tmp_path)
    store.save_split("v.mp4", [SavedSegment(start=0.0, end=1.0)])

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("split_video.local_store.os.replace", boom)
    with pytest.raises(OSError):
        store.save_split("v.mp4", [SavedSegment(start=0.0, end=9.0)])
    monkeypatch.undo()
    assert store.load_split("v.mp4") == [SavedSegment(start=0.0, end=1.0)]


# --- timeline projects ------------------------------------------------------


def test_timeline_round_trip_and_naming(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.save_timeline("show", _project())
    assert (tmp_path / "show.split-video-timeline.json").is_file()
    assert store.load_timeline("show") == _project()
    assert store.timeline_exists("show") and not store.timeline_exists("nope")


def test_missing_timeline_loads_empty_but_load_existing_raises(tmp_path):
    store = LocalProjectStore(tmp_path)
    loaded = store.load_timeline("nope")
    assert len(loaded.tracks) == 1 and loaded.tracks[0].kind == "video" and not loaded.tracks[0].clips
    with pytest.raises(ProjectNotFoundError):
        store.load_existing_timeline("nope")


@pytest.mark.parametrize("content", ["not json", "{}", '{"tracks":[{"id":"t","kind":"bogus"}]}'])
def test_damaged_timeline_loads_empty(tmp_path, content):
    (tmp_path / "x.split-video-timeline.json").write_text(content)
    store = LocalProjectStore(tmp_path)
    assert not store.load_timeline("x").tracks[0].clips
    assert not store.load_existing_timeline("x").tracks[0].clips


@pytest.mark.parametrize("name", ["", "a/b", "..", ".hidden"])
def test_timeline_names_validated_everywhere(tmp_path, name):
    store = LocalProjectStore(tmp_path)
    for call in (store.load_timeline, store.timeline_exists, store.create_timeline):
        with pytest.raises(TimelineError):
            call(name)
    with pytest.raises(TimelineError):
        store.save_timeline(name, _project())


def test_create_refuses_an_existing_name_and_list_is_sorted(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.create_timeline("b")
    store.create_timeline("a")
    with pytest.raises(ProjectExistsError):
        store.create_timeline("a")
    assert store.list_timelines() == ["a", "b"]


# --- compatibility with files written before the store existed ---------------


def test_version_1_split_file_loads_with_defaults_and_resaves_as_v2(tmp_path):
    path = tmp_path / "old.mp4.split-video-project.json"
    path.write_text('{"version":1,"source_file":"old.mp4","segments":[{"start":0,"end":10},{"start":10,"end":20.5}]}')
    store = LocalProjectStore(tmp_path)
    loaded = store.load_split("old.mp4")
    assert loaded == [SavedSegment(start=0.0, end=10.0), SavedSegment(start=10.0, end=20.5)]
    store.save_split("old.mp4", loaded)
    data = json.loads(path.read_text())
    assert data["version"] == 2 and data["source_file"] == "old.mp4"
    assert store.load_split("old.mp4") == loaded


def test_version_2_split_file_round_trips_byte_for_byte_apart_from_the_revision(tmp_path):
    text = (
        '{"version":2,"revision":4,"source_file":"new.mp4","segments":[{"start":0.0,"end":10.0,"label":"Intro",'
        '"color":"#ff0000","included":false,"export_name":"intro"},{"start":10.0,"end":20.5,"label":"",'
        '"color":null,"included":true,"export_name":null}]}'
    )
    path = tmp_path / "new.mp4.split-video-project.json"
    path.write_text(text)
    store = LocalProjectStore(tmp_path)
    loaded = store.load_split("new.mp4")
    assert loaded is not None and loaded[0].label == "Intro" and loaded[0].included is False
    store.save_split("new.mp4", loaded)
    assert path.read_text() == text.replace('"revision":4', '"revision":5')


def test_hand_written_timeline_without_magnetic_flags_loads_and_round_trips(tmp_path):
    path = tmp_path / "old.split-video-timeline.json"
    path.write_text(
        '{"version":1,"tracks":[{"id":"t","kind":"video","clips":[{"id":"c","source":"a.mp4","in":0,"out":3}]},'
        '{"id":"u","kind":"audio","clips":[]}]}'
    )
    store = LocalProjectStore(tmp_path)
    loaded = store.load_existing_timeline("old")
    assert all(t.magnetic for t in loaded.tracks)
    assert loaded.tracks[0].clips[0].gain == 1.0
    store.save_timeline("old", loaded)
    resaved = json.loads(path.read_text())
    assert resaved.pop("revision") == 1  # a file without one was revision 0
    assert resaved == timeline.to_dict(loaded)
    assert resaved["version"] == 2 and resaved["tracks"][0]["magnetic"] is True  # re-saved in the current format
    assert store.load_timeline("old") == loaded
    free = Project(tracks=(Track(id="t", kind="audio", magnetic=False),))
    store.save_timeline("old", free)
    assert store.load_timeline("old") == free
