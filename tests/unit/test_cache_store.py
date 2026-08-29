import os
import time

from split_video.editor import cache_store


def _touch(path, content=b"hello"):
    path.write_bytes(content)
    return path


def test_missing_cache_file_is_a_miss(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    assert cache_store.load(source, "silence") is None


def test_save_then_load_round_trips_the_payload(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    payload = {"threshold": [[1.0, 2.0]]}
    cache_store.save(source, "silence", payload)
    assert cache_store.load(source, "silence") == payload


def test_load_misses_when_source_size_changed(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    cache_store.save(source, "silence", {"a": 1})
    _touch(source, content=b"hello, but longer now")
    assert cache_store.load(source, "silence") is None


def test_load_misses_when_source_mtime_changed(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    cache_store.save(source, "silence", {"a": 1})
    # Same size, different mtime: touch the file with a distinct timestamp.
    later = time.time() + 5
    os.utime(source, (later, later))
    assert cache_store.load(source, "silence") is None


def test_load_ignores_corrupt_cache_file(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    cache_dir = tmp_path / cache_store.CACHE_DIR_NAME
    cache_dir.mkdir()
    (cache_dir / f"{source.name}.silence.json").write_text("not json")
    assert cache_store.load(source, "silence") is None


def test_different_cache_names_do_not_collide(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    cache_store.save(source, "silence", {"kind": "silence"})
    cache_store.save(source, "waveform", {"kind": "waveform"})
    assert cache_store.load(source, "silence") == {"kind": "silence"}
    assert cache_store.load(source, "waveform") == {"kind": "waveform"}


def test_cache_file_lives_in_hidden_sidecar_dir_next_to_source(tmp_path):
    source = _touch(tmp_path / "video.mp4")
    cache_store.save(source, "silence", {"a": 1})
    expected = tmp_path / cache_store.CACHE_DIR_NAME / "video.mp4.silence.json"
    assert expected.exists()
