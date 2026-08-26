import pytest

from split_video.editor.browse import PathEscapesRootError, list_directory, resolve_within_root


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "concert.mp4").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    (tmp_path / ".hidden.mp4").write_bytes(b"")
    sub = tmp_path / "concert_split"
    sub.mkdir()
    (sub / "01 - concert.mp4").write_bytes(b"")
    (sub / "02 - concert.mp4").write_bytes(b"")
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_root_listing_includes_videos_and_dirs_but_not_other_files(tree):
    listing = list_directory(tree)
    names = {e.name for e in listing.entries}
    assert names == {"concert.mp4", "concert_split"}


def test_root_listing_excludes_hidden_entries(tree):
    listing = list_directory(tree)
    names = {e.name for e in listing.entries}
    assert ".hidden.mp4" not in names
    assert ".git" not in names


def test_dirs_sort_before_files_alphabetically(tree):
    (tree / "aardvark.mp4").write_bytes(b"")
    listing = list_directory(tree)
    assert [e.name for e in listing.entries] == ["concert_split", "aardvark.mp4", "concert.mp4"]


def test_root_listing_cwd_and_parent(tree):
    listing = list_directory(tree)
    assert listing.cwd == ""
    assert listing.parent is None


def test_subdirectory_listing_reports_relative_paths(tree):
    listing = list_directory(tree, "concert_split")
    assert listing.cwd == "concert_split"
    assert listing.parent == ""
    paths = {e.path for e in listing.entries}
    assert paths == {"concert_split/01 - concert.mp4", "concert_split/02 - concert.mp4"}


def test_traversal_outside_root_is_rejected(tree):
    with pytest.raises(PathEscapesRootError):
        resolve_within_root(tree, "../")


def test_absolute_path_cannot_escape_root(tree):
    # An absolute-looking rel_path must still resolve inside root, not replace it.
    resolved = resolve_within_root(tree, "/concert_split")
    assert resolved == (tree / "concert_split").resolve()


def test_listing_nonexistent_directory_raises(tree):
    with pytest.raises(NotADirectoryError):
        list_directory(tree, "does-not-exist")
