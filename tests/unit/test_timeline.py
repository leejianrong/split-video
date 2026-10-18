import pytest

from split_video import timeline
from split_video.local_media_store import LocalMediaStore
from split_video.timeline import Clip, Project, TimelineError, Track


def _project():
    clip = Clip(
        id="c1", source="a/one.mp4", in_point=1.0, out_point=6.0, gain=0.5, muted=True, x=0.2, y=0.8, scale=0.4,
        opacity=0.5, brightness=0.1, contrast=1.2, saturation=0.9,
    )  # fmt: skip
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


def test_dict_round_trip():
    assert timeline.from_dict(timeline.to_dict(_project())) == _project()


@pytest.mark.parametrize("data", [{}, {"tracks": [{"id": "t", "kind": "bogus"}]}])
def test_from_dict_rejects_damaged_data(data):
    with pytest.raises((KeyError, ValueError)):
        timeline.from_dict(data)


def test_durations():
    p = _project()
    assert p.tracks[0].clips[0].duration == 5.0
    assert p.duration == 9.0


def test_source_media_id_rejects_escape(tmp_path):
    media = LocalMediaStore(tmp_path)
    assert timeline.source_media_id(media, "a/b.mp4") == "a/b.mp4"
    with pytest.raises(timeline.OutsideRootError):
        timeline.source_media_id(media, "../secret.mp4")
    with pytest.raises(timeline.OutsideRootError):
        timeline.source_media_id(media, "/etc/passwd")


@pytest.mark.parametrize("name", ["", "a/b", "a\\b", "..", ".hidden"])
def test_project_name_validated(name):
    with pytest.raises(TimelineError):
        timeline.validate_name(name)


def test_files_without_a_magnetic_flag_load_as_magnetic():
    data = {"version": 1, "tracks": [{"id": "t", "kind": "video", "clips": []}]}
    assert timeline.from_dict(data).tracks[0].magnetic is True
