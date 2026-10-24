"""Scanning a workspace folder into the asset table, against the in-memory media store."""

import pytest

from split_video import assets, library
from split_video.ffmpeg import MediaInfo
from split_video.jobs import JobCancelledError
from split_video.media_store import InMemoryMediaStore, MediaNotFoundError, MediaOutsideRootError

VIDEO = MediaInfo(duration=12.0, width=320, height=240, has_audio=True, fps=25.0)
AUDIO = MediaInfo(duration=30.0, width=None, height=None, has_audio=True)
MIB = 1 << 20


class CountingStore(InMemoryMediaStore):
    """Counts probes and the bytes read through `open_read`."""

    def __init__(self):
        super().__init__()
        self.probes: list[str] = []
        self.bytes_read = 0

    def probe(self, media_id):
        self.probes.append(media_id)
        return super().probe(media_id)

    def open_read(self, media_id):
        stream = super().open_read(media_id)
        store = self
        read = stream.read

        def counted(n=-1):
            data = read(n)
            store.bytes_read += len(data)
            return data

        stream.read = counted
        return stream


@pytest.fixture
def media():
    store = CountingStore()
    store.add("a.mp4", b"a-bytes", info=VIDEO)
    store.add("sub/deep/b.mp3", b"b-bytes", info=AUDIO)
    return store


def test_new_files_appear_after_a_scan(media):
    assert library.listing(media) == []
    report = library.scan(media)
    assert report.total == 2 and [a.path for a in report.added] == ["a.mp4", "sub/deep/b.mp3"]
    rows = {r["path"]: r for r in library.listing(media)}
    assert set(rows) == {"a.mp4", "sub/deep/b.mp3"}
    video, audio = rows["a.mp4"], rows["sub/deep/b.mp3"]
    assert (video["duration"], video["width"], video["height"], video["fps"]) == (12.0, 320, 240, 25.0)
    assert video["has_video"] and video["has_audio"] and video["size"] == len(b"a-bytes")
    assert not audio["has_video"] and audio["has_audio"] and audio["width"] is None

    media.add("sub/new.mp4", b"new", info=VIDEO)
    report = library.scan(media)
    assert [a.path for a in report.added] == ["sub/new.mp4"] and report.unchanged == 2
    assert len(library.listing(media)) == 3


def test_probe_results_are_cached_and_unchanged_files_are_not_probed_again(media):
    library.scan(media)
    assert sorted(media.probes) == ["a.mp4", "sub/deep/b.mp3"]
    media.probes.clear()
    media.bytes_read = 0
    report = library.scan(media)
    assert media.probes == [] and media.bytes_read == 0  # not probed, not even hashed
    assert report.unchanged == 2 and not report.added and not report.skipped
    library.listing(media)
    assert media.probes == []  # listing reads the table only


def test_a_second_process_sees_the_table_without_probing(media):
    library.scan(media)
    media.probes.clear()
    assert len(assets.list_assets(media)) == 2 and media.probes == []


def test_a_file_that_cannot_be_read_is_skipped_with_a_reason(media):
    media.add("broken.mp4", b"junk")  # no media info: probing fails
    report = library.scan(media)
    assert [a.path for a in report.added] == ["a.mp4", "sub/deep/b.mp3"]
    assert len(report.skipped) == 1 and report.skipped[0]["path"] == "broken.mp4"
    assert "broken.mp4" in report.skipped[0]["reason"]
    assert {r["path"] for r in library.listing(media)} == {"a.mp4", "sub/deep/b.mp3"}


def test_hidden_files_and_other_extensions_are_not_scanned(media):
    media.add(".hidden/secret.mp4", b"h", info=VIDEO)
    media.add(".split-video-cache/x.wav", b"c", info=AUDIO)
    media.add("notes.txt", b"t")
    media.add("clip.split-video-project.json", b"{}")
    assert library.scan(media).total == 2


def test_a_copy_is_recorded_without_a_second_probe(media):
    media.add("backup/a-copy.mp4", b"a-bytes", info=VIDEO)
    report = library.scan(media)
    assert [a.path for a in report.added] == ["a.mp4", "sub/deep/b.mp3"]
    assert report.copies == ["backup/a-copy.mp4"]
    assert "backup/a-copy.mp4" not in media.probes
    assert library.scan(media).unchanged == 3


def test_a_changed_file_is_probed_again_and_a_moved_one_is_not(media):
    library.scan(media)
    media.probes.clear()
    media.rename("a.mp4", "renamed.mp4")
    report = library.scan(media)
    assert report.copies == ["renamed.mp4"] and media.probes == []
    (row,) = [r for r in library.listing(media) if r["duration"] == 12.0]
    assert row["path"] == "renamed.mp4" and row["present"]

    media.add("sub/deep/b.mp3", b"entirely different bytes", info=MediaInfo(5.0, None, None, True), mtime=9.0)
    report = library.scan(media)
    assert [a.path for a in report.added] == ["sub/deep/b.mp3"] and media.probes == ["sub/deep/b.mp3"]


def test_scan_only_a_folder(media):
    report = library.scan(media, "sub")
    assert report.folder == "sub" and [a.path for a in report.added] == ["sub/deep/b.mp3"]
    assert [r["path"] for r in library.listing(media)] == ["sub/deep/b.mp3"]


def test_a_bad_folder_is_an_error_not_an_empty_library(media):
    with pytest.raises(MediaNotFoundError):
        library.scan(media, "nope")
    with pytest.raises(MediaOutsideRootError):
        library.scan(media, "../elsewhere")


def test_progress_counts_files(media):
    seen = []
    library.scan(media, on_progress=lambda done, total, path: seen.append((done, total, path)))
    assert seen == [(1, 2, "a.mp4"), (2, 2, "sub/deep/b.mp3")]


def test_cancelling_keeps_what_was_found(media):
    class Cancel:
        def __init__(self):
            self.calls = 0

        def is_cancelled(self):
            return self.calls > 1

        def raise_if_cancelled(self):
            self.calls += 1
            if self.calls > 1:
                raise JobCancelledError("cancelled")

        def on_cancel(self, callback):
            return lambda: None

    with pytest.raises(JobCancelledError):
        library.scan(media, cancel=Cancel())
    assert [r["path"] for r in library.listing(media)] == ["a.mp4"]


def test_the_table_is_saved_in_batches(media, monkeypatch):
    monkeypatch.setattr(library, "SAVE_EVERY", 1)
    saves = []
    original = media.save_asset_table
    media.save_asset_table = lambda payload: (saves.append(len(payload["assets"])), original(payload))[1]
    library.scan(media)
    assert saves == [1, 2]


def test_a_huge_file_is_identified_by_samples_not_read_in_full():
    store = CountingStore()
    big = 200 * MIB
    store.add("long.mp4", bytes(big), info=MediaInfo(3 * 3600.0, 160, 90, True, 1.0))
    library.scan(store)
    assert store.bytes_read <= 3 * MIB  # first, middle and last MiB
    assert store.probes == ["long.mp4"]
    (row,) = library.listing(store)
    assert row["duration"] == 10800.0 and row["size"] == big
