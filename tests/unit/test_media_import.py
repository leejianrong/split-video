"""The rules of importing an uploaded file, against the in-memory media store."""

import pytest

from split_video import media_import
from split_video.media_import import ImportError_, IncomingFile, clean_name, free_id
from split_video.media_store import InMemoryMediaStore


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("gig.mp4", "gig.mp4"),
        ("C:\\Users\\me\\Videos\\Gig Night.MKV", "Gig Night.MKV"),
        ("../../etc/clip.mov", "clip.mov"),
        ("  spaced.mp3 ", "spaced.mp3"),
    ],
)
def test_clean_name_keeps_only_the_file_name(given, stored):
    assert clean_name(given) == stored


@pytest.mark.parametrize("given", ["", "   ", ".hidden.mp4", "dir/", "..", "x" * 200 + ".mp4"])
def test_clean_name_refuses_names_that_are_not_plain(given):
    with pytest.raises(ImportError_) as err:
        clean_name(given)
    assert err.value.error_code == "upload.invalid_name"


def test_clean_name_refuses_other_file_types():
    with pytest.raises(ImportError_) as err:
        clean_name("notes.txt")
    assert err.value.error_code == "upload.unsupported_type"
    assert ".mp4" in err.value.details["supported"] and ".wav" in err.value.details["supported"]


def test_free_id_numbers_a_name_that_is_taken():
    media = InMemoryMediaStore()
    media.add("shows/gig.mp4", b"x")
    media.add("shows/gig (2).mp4", b"x")
    assert free_id(media, "shows", "gig.mp4") == "shows/gig (3).mp4"
    assert free_id(media, "shows", "other.mp4") == "shows/other.mp4"
    assert free_id(media, "", "gig.mp4") == "gig.mp4"


def test_an_upload_that_cannot_be_probed_leaves_nothing_behind():
    media = InMemoryMediaStore(materialise=True)
    with pytest.raises(ImportError_) as err, IncomingFile(media, "", "broken.mp4") as incoming:
        incoming.write(b"not a video")
        incoming.finish()
    assert err.value.error_code == "media.unreadable"
    assert media.ids() == []


def test_an_empty_upload_is_refused_and_removed():
    media = InMemoryMediaStore(materialise=True)
    with pytest.raises(ImportError_) as err, IncomingFile(media, "", "empty.mp4") as incoming:
        incoming.finish()
    assert err.value.error_code == "upload.empty"
    assert media.ids() == []


def test_an_interrupted_upload_removes_the_partial_file():
    media = InMemoryMediaStore(materialise=True)
    with pytest.raises(RuntimeError), IncomingFile(media, "", "cut.mp4") as incoming:
        incoming.write(b"half")
        raise RuntimeError("connection dropped")
    assert media.ids() == []


def test_the_module_names_its_error_codes_in_the_catalogue():
    from split_video.error_catalogue import REGISTRY

    for code in ("upload.invalid_name", "upload.unsupported_type", "upload.empty"):
        assert code in REGISTRY
    assert media_import.MAX_NAME_LENGTH > 0
