import pytest

from split_video import timeline
from split_video.timeline import Clip, Project, TimelineError, Track


def _project():
    clip = Clip(id="c1", source="a/one.mp4", in_point=1.0, out_point=6.0, start=0.0, gain=0.5, muted=True)
    clip2 = Clip(
        id="c2",
        source="two.mp4",
        in_point=0.0,
        out_point=4.0,
        start=5.0,
        transition="crossfade",
        transition_duration=1.0,
    )
    return Project(tracks=(Track(id="t1", kind="video", clips=(clip, clip2)), Track(id="t2", kind="audio")))


def test_round_trip(tmp_path):
    path = timeline.project_path(tmp_path, "show")
    timeline.save(path, _project())
    assert timeline.load(path) == _project()


def test_missing_file_loads_empty_video_project(tmp_path):
    loaded = timeline.load(tmp_path / "nope.split-video-timeline.json")
    assert len(loaded.tracks) == 1 and loaded.tracks[0].kind == "video" and not loaded.tracks[0].clips


@pytest.mark.parametrize("content", ["not json", "{}", '{"tracks":[{"id":"t","kind":"bogus"}]}'])
def test_corrupt_file_loads_empty_project(tmp_path, content):
    path = tmp_path / "x.split-video-timeline.json"
    path.write_text(content)
    assert not timeline.load(path).tracks[0].clips


def test_durations():
    p = _project()
    assert p.tracks[0].clips[0].duration == 5.0
    assert p.duration == 9.0


def test_resolve_source_rejects_escape(tmp_path):
    assert timeline.resolve_source(tmp_path, "a/b.mp4") == (tmp_path / "a/b.mp4").resolve()
    with pytest.raises(TimelineError):
        timeline.resolve_source(tmp_path, "../secret.mp4")
    with pytest.raises(TimelineError):
        timeline.resolve_source(tmp_path, "/etc/passwd")


def test_relative_source_rejects_outside(tmp_path):
    assert timeline.relative_source(tmp_path, tmp_path / "a" / "b.mp4") == "a/b.mp4"
    with pytest.raises(TimelineError):
        timeline.relative_source(tmp_path, tmp_path.parent / "b.mp4")


@pytest.mark.parametrize("name", ["", "a/b", "..", ".hidden"])
def test_project_name_validated(tmp_path, name):
    with pytest.raises(TimelineError):
        timeline.project_path(tmp_path, name)


def test_list_projects(tmp_path):
    timeline.save(timeline.project_path(tmp_path, "b"), _project())
    timeline.save(timeline.project_path(tmp_path, "a"), _project())
    assert timeline.list_projects(tmp_path) == ["a", "b"]


def test_files_without_a_magnetic_flag_load_as_magnetic_and_free_tracks_round_trip(tmp_path):
    path = tmp_path / "old.split-video-timeline.json"
    path.write_text('{"version":1,"tracks":[{"id":"t","kind":"video","clips":[]}]}')
    assert timeline.load(path).tracks[0].magnetic is True
    free = Project(tracks=(Track(id="t", kind="audio", magnetic=False),))
    timeline.save(path, free)
    assert timeline.load(path) == free
