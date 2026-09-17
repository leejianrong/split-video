from split_video.editor import project_store
from split_video.editor.project_store import SavedSegment


def _touch(path, content=b"hello"):
    path.write_bytes(content)
    return path


def test_missing_project_file_is_a_miss(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    assert project_store.load(source) is None


def test_save_then_load_round_trips_segments(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    segments = [SavedSegment(start=0.0, end=10.0), SavedSegment(start=10.0, end=25.5)]
    project_store.save(source, segments)
    assert project_store.load(source) == segments


def test_save_overwrites_a_previous_save(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    project_store.save(source, [SavedSegment(start=0.0, end=5.0)])
    project_store.save(source, [SavedSegment(start=0.0, end=8.0), SavedSegment(start=8.0, end=12.0)])
    assert project_store.load(source) == [SavedSegment(start=0.0, end=8.0), SavedSegment(start=8.0, end=12.0)]


def test_load_ignores_corrupt_project_file(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    (tmp_path / f"{source.name}.split-video-project.json").write_text("not json")
    assert project_store.load(source) is None


def test_project_file_lives_visibly_next_to_source(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    project_store.save(source, [SavedSegment(start=0.0, end=1.0)])
    expected = tmp_path / "video.mp4.split-video-project.json"
    assert expected.exists()


def test_different_sources_do_not_collide(tmp_path):
    a = _touch(tmp_path / "a.mp4", content=b"a")
    b = _touch(tmp_path / "b.mp4", content=b"b")
    project_store.save(a, [SavedSegment(start=0.0, end=1.0)])
    project_store.save(b, [SavedSegment(start=0.0, end=2.0)])
    assert project_store.load(a) == [SavedSegment(start=0.0, end=1.0)]
    assert project_store.load(b) == [SavedSegment(start=0.0, end=2.0)]
