"""Export modes on the in-memory stores, with ffmpeg replaced by fakes (SPL-117)."""

import threading
import time

import pytest

from split_video import export_modes, project, render, timeline_service, transcribe
from split_video.errors import ProjectError
from split_video.ffmpeg import ExtractError, MediaInfo
from split_video.jobs import RENDER, InProcessJobQueue
from split_video.media_store import InMemoryMediaStore
from split_video.render_queue import batch_status, cancel_renders
from split_video.store import InMemoryProjectStore, SavedSegment
from split_video.timeline import TimelineError

VIDEO = MediaInfo(duration=60.0, width=1280, height=720, has_audio=True)


@pytest.fixture
def media():
    store = InMemoryMediaStore()
    store.add("gig.mp4", b"gig", info=VIDEO)
    return store


@pytest.fixture
def store(media):
    store = InMemoryProjectStore()
    project.save_segments(
        store,
        "gig.mp4",
        [
            SavedSegment(0.0, 10.0, label="Intro"),
            SavedSegment(10.0, 20.0, label="Song", export_name="the-song"),
            SavedSegment(20.0, 30.0, included=False),
            SavedSegment(30.0, 40.0),
        ],
        media=media,
    )
    for name in ("clip1", "clip2"):
        timeline_service.create_project(store, name)
        timeline_service.add_clip(store, media, name, "gig.mp4", in_point=5.0, out_point=15.0)
    return store


@pytest.fixture
def jobs():
    queue = InProcessJobQueue(max_workers=3, kind_limits={RENDER: 2})
    yield queue
    queue.shutdown()


class Fakes:
    def __init__(self, media):
        self.media = media
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.copies: list[tuple[float, float]] = []
        self.fail: set[str] = set()
        self.release = threading.Event()
        self.hold = False

    def render(self, plan, inputs, out_path, total, progress, cancel=None, preset=None, stall_timeout=None):
        name = out_path.rsplit("/", 1)[-1]
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            self.media.write_bytes(out_path.split("/in-memory-media/")[-1], b"x")
            if name in self.fail:
                raise ExtractError(f"ffmpeg failed rendering '{name}'")
            while self.hold and not self.release.is_set():
                cancel.raise_if_cancelled()
                time.sleep(0.01)
            time.sleep(0.1)
            progress(1.0)
        finally:
            with self.lock:
                self.running -= 1

    def extract(self, path, start, end, out_path, precise, output_format=None, cancel=None):
        self.copies.append((start, end))


@pytest.fixture
def fakes(media, monkeypatch):
    fake = Fakes(media)
    monkeypatch.setattr(render, "_run_ffmpeg", fake.render)
    monkeypatch.setattr(export_modes, "extract_segment", fake.extract)
    return fake


def _wait(jobs, batch):
    for job in batch["jobs"]:
        jobs.wait(job["job_id"], 15)
    return batch_status(jobs, batch["batch_id"])


def _states(batch):
    return [j["state"] for j in batch["jobs"]]


def test_segments_write_the_included_segments_named_by_export_name_or_label(store, media, jobs, fakes):
    batch = export_modes.submit_export(jobs, store, media, "segments", video="gig.mp4")
    assert batch["mode"] == "segments" and batch["total"] == 3
    done = _wait(jobs, batch)
    assert _states(done) == ["done"] * 3
    names = [j["output"] for j in done["jobs"]]
    # The export name wins, then the label ("Intro.mp4"), then the numbered default of the split export.
    assert names == ["Intro.mp4", "the-song.mp4", names[2]]
    assert names[2] == "03 - gig.mp4"
    assert fakes.copies == [(0.0, 10.0), (10.0, 20.0), (30.0, 40.0)]
    assert media.exists("gig_split/" + names[1])


def test_segments_labels_become_safe_distinct_file_names(media, jobs, fakes):
    store = InMemoryProjectStore()
    project.save_segments(
        store,
        "gig.mp4",
        [
            SavedSegment(0.0, 10.0, label="Short: A/B?"),
            SavedSegment(10.0, 20.0, label="Song"),
            SavedSegment(20.0, 30.0, label="song"),
            SavedSegment(30.0, 40.0, label="..."),
        ],
        media=media,
    )
    batch = export_modes.submit_export(jobs, store, media, "segments", video="gig.mp4")
    names = [j["output"] for j in _wait(jobs, batch)["jobs"]]
    assert names[:3] == ["Short - A - B.mp4", "Song.mp4", "song (2).mp4"]
    assert names[3].startswith("04 - gig")


def test_segments_only_overrides_include_and_bad_numbers_are_a_request_error(store, media, jobs, fakes):
    batch = export_modes.submit_export(jobs, store, media, "segments", video="gig.mp4", only=[3])
    assert batch["total"] == 1
    with pytest.raises(ProjectError):
        export_modes.submit_export(jobs, store, media, "segments", video="gig.mp4", only=[9])


def test_segments_one_existing_file_fails_alone(store, media, jobs, fakes):
    media.add("gig_split/the-song.mp4", b"old")
    batch = export_modes.submit_export(jobs, store, media, "segments", video="gig.mp4")
    assert _states(batch)[1] == "failed"
    assert batch["jobs"][1]["error"]["code"] == "render.output_exists" and batch["jobs"][1]["error"]["fix"]
    done = _wait(jobs, batch)
    assert _states(done) == ["done", "failed", "done"]
    assert media.read("gig_split/the-song.mp4") == b"old"
    again = export_modes.submit_export(jobs, store, media, "segments", video="gig.mp4", overwrite=True)
    assert _states(_wait(jobs, again)) == ["done"] * 3


def test_segments_with_a_preset_render_each_one(store, media, jobs, fakes):
    batch = export_modes.submit_export(
        jobs, store, media, "segments", video="gig.mp4", options=export_modes.RenderOptions(preset="draft-720")
    )
    done = _wait(jobs, batch)
    assert _states(done) == ["done"] * 3 and fakes.copies == []
    assert all(j["output"].endswith(".mp4") for j in done["jobs"])
    assert done["jobs"][0]["result"]["preset"] == "draft-720" and done["jobs"][0]["result"]["duration"] == 10.0


def test_a_copy_that_fails_is_reported_and_the_rest_carry_on(store, media, jobs, fakes, monkeypatch):
    def extract(path, start, end, out_path, precise, output_format=None, cancel=None):
        if start == 10.0:
            raise ExtractError("ffmpeg failed for 'the-song.mp4': boom")

    monkeypatch.setattr(export_modes, "extract_segment", extract)
    done = _wait(jobs, export_modes.submit_export(jobs, store, media, "segments", video="gig.mp4"))
    assert _states(done) == ["done", "failed", "done"]
    assert done["jobs"][1]["error"]["code"] == "ffmpeg.failed"
    assert not any("the-song" in i for i in media.ids())


def test_a_missing_source_fails_that_item_only(store, media, jobs, fakes):
    media.remove("gig.mp4")
    done = _wait(jobs, export_modes.submit_export(jobs, store, media, "highlights", timelines=["clip1"]))
    assert _states(done) == ["failed"] and done["jobs"][0]["error"]["code"] == "source.not_found"


def test_timeline_is_named_after_the_project(store, media, jobs, fakes):
    done = _wait(jobs, export_modes.submit_export(jobs, store, media, "timeline", project_name="clip1"))
    assert [j["output"] for j in done["jobs"]] == ["clip1.mp4"] and _states(done) == ["done"]
    assert media.exists("clip1.mp4")
    # A recording's own timeline is named after the recording, without its extension, and sits beside it.
    gig = _wait(jobs, export_modes.submit_export(jobs, store, media, "timeline", video="gig.mp4"))
    assert [j["output"] for j in gig["jobs"]] == ["gig.mp4"] and _states(gig) == ["failed"]
    assert gig["jobs"][0]["error"]["code"] == "render.output_exists"  # the source itself: never overwritten
    other = _wait(
        jobs, export_modes.submit_export(jobs, store, media, "timeline", video="gig.mp4", output="gig-final.mp4")
    )
    assert _states(other) == ["done"]


def test_timeline_never_overwrites_its_source_even_with_overwrite(store, media, jobs, fakes):
    batch = export_modes.submit_export(jobs, store, media, "timeline", video="gig.mp4", overwrite=True)
    assert _states(batch) == ["failed"] and "source" in batch["jobs"][0]["error"]["message"]
    assert media.read("gig.mp4") == b"gig"


def test_highlights_are_vertical_shorts_and_unlisted_timelines_are_not_exported(store, media, jobs, fakes):
    batch = export_modes.submit_export(jobs, store, media, "highlights", timelines=["clip1", "nope", "clip2"])
    done = _wait(jobs, batch)
    assert _states(done) == ["done", "failed", "done"]
    assert done["jobs"][0]["result"]["preset"] == "vertical-1080" and done["jobs"][0]["result"]["fit"] == "crop"
    assert (done["jobs"][0]["result"]["width"], done["jobs"][0]["result"]["height"]) == (1080, 1920)
    assert done["jobs"][1]["error"]["code"] == "timeline.not_found"
    assert media.exists("clip1.mp4") and media.exists("clip2.mp4")
    assert done["jobs"][0]["result"].get("captions") is None  # no transcript


def test_highlights_get_captions_when_a_transcript_exists(store, media, jobs, fakes, monkeypatch):
    monkeypatch.setattr(transcribe, "load", lambda m, asset_id: {"segments": []})
    seen = []
    real = export_modes.render_work

    def spy(*args, **kwargs):
        seen.append(args[-1])
        return real(*args, **kwargs)

    monkeypatch.setattr(export_modes, "render_work", spy)
    monkeypatch.setattr(
        render,
        "render",
        lambda *a, **k: {
            "preset": "vertical-1080",
            "fit": "crop",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "captions": None,
        },
    )
    _wait(jobs, export_modes.submit_export(jobs, store, media, "highlights", timelines=["clip1"]))
    assert seen[0].style == "shorts"
    seen.clear()
    off = export_modes.RenderOptions(captions="off")
    _wait(
        jobs,
        export_modes.submit_export(jobs, store, media, "highlights", timelines=["clip1"], options=off, overwrite=True),
    )
    assert seen[0] is None


def test_request_errors_are_raised_before_anything_is_queued(store, media, jobs, fakes):
    for kwargs, code in [
        ({"mode": "nope"}, "export.invalid"),
        ({"mode": "segments"}, "export.invalid"),
        ({"mode": "timeline"}, "export.invalid"),
        ({"mode": "timeline", "video": "gig.mp4", "project_name": "clip1"}, "export.invalid"),
        ({"mode": "highlights"}, "export.invalid"),
        ({"mode": "highlights", "timelines": []}, "export.invalid"),
    ]:
        with pytest.raises(export_modes.ExportError) as err:
            export_modes.submit_export(jobs, store, media, **kwargs)
        assert err.value.error_code == code
    with pytest.raises(TimelineError):
        export_modes.submit_export(
            jobs, store, media, "timeline", project_name="clip1", options=export_modes.RenderOptions(preset="bogus")
        )
    assert jobs.list(kind=RENDER) == []


def test_concurrency_is_bounded_and_cancel_stops_the_rest(store, media, jobs, fakes):
    fakes.hold = True
    batch = export_modes.submit_export(
        jobs, store, media, "highlights", timelines=["clip1", "clip2"], options=export_modes.RenderOptions()
    )
    batch = export_modes.submit_export(
        jobs, store, media, "segments", video="gig.mp4", options=export_modes.RenderOptions(preset="draft-720")
    )
    deadline = time.monotonic() + 10
    while fakes.running < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fakes.running == 2
    time.sleep(0.2)
    assert fakes.max_running == 2
    cancelled = cancel_renders(jobs, batch_id=batch["batch_id"])
    assert cancelled["counts"]["cancelled"] == 3 and cancelled["finished"]
    fakes.release.set()
