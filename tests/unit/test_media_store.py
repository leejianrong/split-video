"""The MediaStore contract, checked against both implementations, plus the
local store's on-disk layout (which existing caches and sidecars depend on)."""

import json
import os
import time

import pytest

from split_video.ffmpeg import MediaInfo, ProbeError
from split_video.local_media_store import CACHE_DIR_NAME, LocalMediaStore
from split_video.media_store import (
    InMemoryMediaStore,
    MediaNotFoundError,
    MediaOutsideRootError,
    MediaStore,
)

INFO = MediaInfo(duration=12.0, width=320, height=240, has_audio=True)


def _tree(store, files):
    """Create `files` (id -> bytes) in either kind of store."""
    for media_id, data in files.items():
        if isinstance(store, InMemoryMediaStore):
            store.add(media_id, data, info=INFO)
        else:
            path = store.root / media_id
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


@pytest.fixture(params=["local", "memory"])
def store(request, tmp_path):
    return LocalMediaStore(tmp_path) if request.param == "local" else InMemoryMediaStore()


@pytest.fixture
def tree(store):
    _tree(
        store,
        {
            "concert.mp4": b"",
            "notes.txt": b"",
            ".hidden.mp4": b"",
            "concert_split/01 - concert.mp4": b"",
            "concert_split/02 - concert.mp4": b"",
            ".git/config": b"",
        },
    )
    return store


def test_both_implementations_satisfy_the_interface(tmp_path):
    assert isinstance(LocalMediaStore(tmp_path), MediaStore)
    assert isinstance(InMemoryMediaStore(), MediaStore)


def test_root_listing_has_videos_and_dirs_but_no_other_or_hidden_entries(tree):
    listing = tree.list_dir()
    assert {e.name for e in listing.entries} == {"concert.mp4", "concert_split"}
    assert (listing.cwd, listing.parent) == ("", None)


def test_dirs_sort_before_files_alphabetically(tree):
    _tree(tree, {"aardvark.mp4": b""})
    assert [e.name for e in tree.list_dir().entries] == ["concert_split", "aardvark.mp4", "concert.mp4"]


def test_subdirectory_listing_reports_ids_cwd_and_parent(tree):
    listing = tree.list_dir("concert_split")
    assert (listing.cwd, listing.parent) == ("concert_split", "")
    assert {e.path for e in listing.entries} == {"concert_split/01 - concert.mp4", "concert_split/02 - concert.mp4"}


def test_audio_files_are_listed_for_audio_tracks(store):
    _tree(store, {"song.mp3": b"", "voice.WAV": b""})
    assert {e.name for e in store.list_dir().entries} == {"song.mp3", "voice.WAV"}


def test_listing_a_missing_directory_or_a_file_is_not_found(tree):
    for path in ("does-not-exist", "concert.mp4"):
        with pytest.raises(MediaNotFoundError):
            tree.list_dir(path)


def test_traversal_outside_the_root_is_rejected(tree):
    for bad in ("../", "..", "a/../../x.mp4"):
        with pytest.raises(MediaOutsideRootError):
            tree.resolve(bad)
    with pytest.raises(MediaOutsideRootError):
        tree.list_dir("..")


def test_a_leading_slash_means_root_relative_unless_strict(tree):
    assert tree.resolve("/concert_split") == "concert_split"
    assert tree.resolve("") == ""
    assert tree.resolve("concert_split/../concert.mp4") == "concert.mp4"
    with pytest.raises(MediaOutsideRootError):
        tree.resolve("/etc/passwd", strict=True)
    assert tree.resolve("concert.mp4", strict=True) == "concert.mp4"


def test_exists_is_file_stat_and_probe(tree):
    assert tree.is_file("concert.mp4") and tree.exists("concert_split") and not tree.is_file("concert_split")
    assert not tree.exists("nope.mp4")
    assert tree.stat("concert.mp4").size == 0
    with pytest.raises(MediaNotFoundError):
        tree.stat("nope.mp4")
    with pytest.raises(MediaNotFoundError):
        tree.probe("nope.mp4")


def test_id_helpers(store):
    assert store.name("a/b/gig.mp4") == "gig.mp4"
    assert store.join("a/b", "x.mp4") == "a/b/x.mp4" and store.join("", "x.mp4") == "x.mp4"
    assert store.export_dir("a/b/gig.mp4") == "a/b/gig_split" and store.export_dir("gig.mp4") == "gig_split"


def test_write_bytes_and_open_read_round_trip(store):
    store.write_bytes("out/manifest.json", b"hello")
    with store.open_read("out/manifest.json") as f:
        f.seek(2)
        assert f.read() == b"llo"
    with pytest.raises(MediaNotFoundError):
        store.open_read("out/missing")


def test_delete_removes_the_media_and_ignores_a_missing_one(store):
    store.write_bytes("sub/gone.mp4", b"x")
    store.delete("sub/gone.mp4")
    assert not store.exists("sub/gone.mp4")
    store.delete("sub/gone.mp4")  # already gone: not an error
    store.delete("never-existed.mp4")


def test_write_local_commits_what_ffmpeg_wrote(tmp_path):
    for store in (LocalMediaStore(tmp_path / "disk"), InMemoryMediaStore(materialise=True)):
        with store.write_local("renders/out.mp4") as path:
            path.write_bytes(b"video")
        with store.open_read("renders/out.mp4") as f:
            assert f.read() == b"video"


def test_a_failed_write_local_commits_nothing():
    store = InMemoryMediaStore(materialise=True)
    with pytest.raises(RuntimeError), store.write_local("out.mp4") as path:
        path.write_bytes(b"half")
        raise RuntimeError("ffmpeg died")
    assert not store.exists("out.mp4")


def test_local_path_gives_ffmpeg_the_media_bytes(store):
    _tree(store, {"a.mp4": b"payload"})
    if isinstance(store, InMemoryMediaStore):
        store = InMemoryMediaStore(materialise=True)
        _tree(store, {"a.mp4": b"payload"})
    with store.local_path("a.mp4") as path:
        assert path.read_bytes() == b"payload"


# --- derived caches ------------------------------------------------------------------


def test_cache_round_trips_and_names_do_not_collide(store):
    _tree(store, {"video.mp4": b"hello"})
    assert store.load_cache("video.mp4", "silence") is None
    store.save_cache("video.mp4", "silence", {"-35.0": [[1.0, 2.0]]})
    store.save_cache("video.mp4", "waveform", {"kind": "waveform"})
    assert store.load_cache("video.mp4", "silence") == {"-35.0": [[1.0, 2.0]]}
    assert store.load_cache("video.mp4", "waveform") == {"kind": "waveform"}


def test_cache_misses_when_the_media_changed_size(store):
    _tree(store, {"video.mp4": b"hello"})
    store.save_cache("video.mp4", "silence", {"a": 1})
    _tree(store, {"video.mp4": b"hello, but longer now"})
    assert store.load_cache("video.mp4", "silence") is None


def test_cache_misses_when_the_media_mtime_changed_but_not_its_size(store, tmp_path):
    _tree(store, {"video.mp4": b"hello"})
    store.save_cache("video.mp4", "silence", {"a": 1})
    if isinstance(store, InMemoryMediaStore):
        store.add("video.mp4", b"hello", info=INFO, mtime=99.0)
    else:
        later = time.time() + 5
        os.utime(tmp_path / "video.mp4", (later, later))
    assert store.load_cache("video.mp4", "silence") is None


# --- the local store's layout --------------------------------------------------------


def test_cache_files_live_in_the_hidden_sidecar_dir_next_to_the_video(tmp_path):
    store = LocalMediaStore(tmp_path)
    _tree(store, {"sub/video.mp4": b"hello"})
    store.save_cache("sub/video.mp4", "silence", {"a": 1})
    assert (tmp_path / "sub" / CACHE_DIR_NAME / "video.mp4.silence.json").exists()
    assert CACHE_DIR_NAME == ".split-video-cache"
    assert not (tmp_path / CACHE_DIR_NAME).exists()  # nothing at the root for a video in a subdir
    assert not list((tmp_path / "sub" / CACHE_DIR_NAME).glob("*.tmp"))


def test_a_cache_written_by_the_old_cache_store_is_still_read(tmp_path):
    """Fixture bytes exactly as editor/cache_store.save wrote them before the MediaStore."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"hello")
    stat = video.stat()
    cache_dir = tmp_path / ".split-video-cache"
    cache_dir.mkdir()
    old = {"fingerprint": {"size": stat.st_size, "mtime": stat.st_mtime}, "payload": {"-35.0": [[1.0, 2.0]]}}
    (cache_dir / "video.mp4.silence.json").write_text(json.dumps(old, separators=(",", ":")))
    assert LocalMediaStore(tmp_path).load_cache("video.mp4", "silence") == {"-35.0": [[1.0, 2.0]]}


def test_a_saved_cache_is_byte_identical_to_what_the_old_cache_store_wrote(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"hello")
    payload = {"buckets": [[-0.5, 0.5]]}
    LocalMediaStore(tmp_path).save_cache("video.mp4", "waveform", payload)
    stat = video.stat()
    expected = json.dumps(
        {"fingerprint": {"size": stat.st_size, "mtime": stat.st_mtime}, "payload": payload}, separators=(",", ":")
    )
    assert (tmp_path / ".split-video-cache" / "video.mp4.waveform.json").read_text() == expected


def test_a_corrupt_cache_file_is_a_miss(tmp_path):
    (tmp_path / "video.mp4").write_bytes(b"hello")
    cache_dir = tmp_path / CACHE_DIR_NAME
    cache_dir.mkdir()
    (cache_dir / "video.mp4.silence.json").write_text("not json")
    assert LocalMediaStore(tmp_path).load_cache("video.mp4", "silence") is None


def test_local_resolve_follows_symlinks_and_refuses_ones_that_leave_the_root(tmp_path):
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "real" / "a.mp4").write_bytes(b"x")
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"x")
    (root / "link.mp4").symlink_to(root / "real" / "a.mp4")
    (root / "escape.mp4").symlink_to(outside)
    store = LocalMediaStore(root)
    assert store.resolve("link.mp4") == "real/a.mp4"
    with pytest.raises(MediaOutsideRootError):
        store.resolve("escape.mp4")


def test_local_resolve_user_path_accepts_absolute_paths_inside_the_root(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    store = LocalMediaStore(tmp_path)
    assert store.resolve_user_path(str(tmp_path / "a.mp4")) == "a.mp4"
    assert store.resolve_user_path("a.mp4") == "a.mp4"
    with pytest.raises(MediaOutsideRootError):
        store.resolve_user_path("/etc/passwd")


def test_local_display_path_and_export_dir(tmp_path):
    store = LocalMediaStore(tmp_path)
    assert store.display_path(store.export_dir("sub/show.mp4")) == str(tmp_path / "sub" / "show_split")


def test_local_probe_is_remembered_per_mtime(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("split_video.local_media_store.probe_media", lambda path: calls.append(path) or INFO)
    (tmp_path / "a.mp4").write_bytes(b"x")
    store = LocalMediaStore(tmp_path)
    assert store.probe("a.mp4") == INFO and store.probe("a.mp4") == INFO
    assert len(calls) == 1
    os.utime(tmp_path / "a.mp4", (1, 1))
    store.probe("a.mp4")
    assert len(calls) == 2


def test_in_memory_probe_without_info_is_a_probe_error():
    store = InMemoryMediaStore()
    store.add("a.mp4", b"x")
    with pytest.raises(ProbeError):
        store.probe("a.mp4")
