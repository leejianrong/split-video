"""The timeline project is the one model: a split project opens as a timeline and migrates on first save (SPL-116).

Runs on real legacy split files (`tests/fixtures/split_legacy/`, format versions 1 and 2) against the in-memory
and the local stores.
"""

import json
import shutil
from pathlib import Path

import pytest

from split_video import project, split_model, timeline
from split_video import timeline_service as service
from split_video.errors import ProjectError, RevisionConflictError
from split_video.ffmpeg import MediaInfo
from split_video.local_media_store import LocalMediaStore
from split_video.local_store import SPLIT_SUFFIX, TIMELINE_SUFFIX, LocalProjectStore
from split_video.media_store import InMemoryMediaStore
from split_video.store import InMemoryProjectStore, SavedSegment
from split_video.timeline import Clip

LEGACY = Path(__file__).resolve().parents[1] / "fixtures" / "split_legacy"
FIXTURES = ["v1_minimal", "v2_labelled", "v2_empty", "v2_unparseable", "v2_gaps_and_overlap"]
VIDEO = "gig.mp4"
DURATION = 21.0


def legacy_file(name: str) -> dict:
    return json.loads((LEGACY / f"{name}.split-video-project.json").read_text())


def expected_segments(name: str) -> list[SavedSegment] | None:
    """What the pre-change code read from the legacy file (the parse the local store always did)."""
    try:
        return [
            SavedSegment(
                start=float(s["start"]),
                end=float(s["end"]),
                label=str(s.get("label", "")),
                color=s.get("color"),
                included=bool(s.get("included", True)),
                export_name=s.get("export_name"),
            )
            for s in legacy_file(name)["segments"]
        ]
    except (KeyError, TypeError, ValueError):
        return None


@pytest.fixture(params=["memory", "local"])
def env(request, tmp_path):
    """(store, media, legacy installer, timeline file or None) for each store implementation."""
    if request.param == "memory":
        store = InMemoryProjectStore()
        media = InMemoryMediaStore()
        media.add(VIDEO, b"gig-bytes", info=MediaInfo(DURATION, 320, 240, True))

        def install(name: str) -> None:
            revision = legacy_file(name).get("revision", 0)
            segments = expected_segments(name)
            if segments is None:
                pytest.skip("an unparseable file is only a local-file concern")
            # the in-memory store only authors legacy projects through save_split: reach the file's revision
            for _ in range(max(revision, 1)):
                store.save_split(VIDEO, segments or [])

        return store, media, install, None

    root = tmp_path / "shows"
    root.mkdir()
    (root / VIDEO).write_bytes(b"not really a video")
    store = LocalProjectStore(root)
    media = LocalMediaStore(root)

    def install_local(name: str) -> None:
        shutil.copy(LEGACY / f"{name}.split-video-project.json", root / f"{VIDEO}{SPLIT_SUFFIX}")

    return store, media, install_local, root / f"{VIDEO}{TIMELINE_SUFFIX}"


@pytest.fixture
def probed(monkeypatch):
    """Local media is not a real video: every probe says 21 s."""
    info = MediaInfo(DURATION, 320, 240, True)
    monkeypatch.setattr(LocalMediaStore, "probe", lambda self, media_id: info)


# --- the mapping -------------------------------------------------------------------------


@pytest.mark.parametrize("name", [f for f in FIXTURES if f != "v2_unparseable"])
def test_a_legacy_project_converts_to_a_timeline_with_the_same_segments(name):
    segments = expected_segments(name)
    converted = split_model.from_legacy(VIDEO, segments)
    track = converted.tracks[0]
    assert track.kind == "video" and track.magnetic and len(converted.tracks) == 1
    assert all(c.source == VIDEO and c.kind == "media" for c in track.clips)
    assert split_model.to_segments(converted) == segments
    # laid out back to back on the main track
    assert [c.start for c in track.clips] == pytest.approx(
        [sum(c.duration for c in track.clips[:i]) for i in range(len(track.clips))]
    )
    # and survives the file format
    assert split_model.to_segments(timeline.from_dict(timeline.to_dict(converted))) == segments


def test_conversion_is_deterministic():
    segments = expected_segments("v2_labelled")
    assert split_model.from_legacy(VIDEO, segments) == split_model.from_legacy(VIDEO, segments)


def test_clip_split_metadata_is_written_only_when_set():
    plain = timeline.to_dict(timeline.Project(tracks=(timeline.Track(id="t", clips=(Clip("c", "a.mp4", 0, 1),)),)))
    clip = plain["tracks"][0]["clips"][0]
    assert plain["version"] == 2
    assert not {"label", "label_color", "included", "export_name"} & clip.keys()
    tagged = Clip("c", "a.mp4", 0, 1, label="x", label_color="#112233", included=False, export_name="N")
    back = timeline.from_dict(timeline.to_dict(timeline.Project(tracks=(timeline.Track(id="t", clips=(tagged,)),))))
    assert back.tracks[0].clips[0] == tagged


def test_a_changed_segment_keeps_unchanged_clips_and_inherits_properties():
    base = split_model.from_legacy(VIDEO, [SavedSegment(0.0, 10.0), SavedSegment(10.0, 20.0)])
    tuned = timeline.Project(
        tracks=(
            timeline.Track(
                id=base.tracks[0].id,
                clips=tuple(
                    c if i else Clip(**{**c.__dict__, "gain": 0.5}) for i, c in enumerate(base.tracks[0].clips)
                ),
            ),
        )
    )
    after = split_model.apply_segments(
        tuned, VIDEO, [SavedSegment(0.0, 5.0), SavedSegment(5.0, 10.0), SavedSegment(10.0, 20.0)]
    )
    clips = after.tracks[0].clips
    assert [c.id for c in clips][2] == tuned.tracks[0].clips[1].id  # unchanged segment: same clip
    assert clips[0].id == tuned.tracks[0].clips[0].id and clips[0].gain == 0.5  # first half keeps the id
    assert clips[1].gain == 0.5 and clips[1].id not in {c.id for c in tuned.tracks[0].clips}  # second half: new id


def test_other_tracks_survive_a_split_edit_but_a_mixed_main_track_is_refused():
    base = split_model.from_legacy(VIDEO, [SavedSegment(0.0, 21.0)])
    overlay = timeline.Track(
        id="o", kind="video", magnetic=False, clips=(Clip("t", "", 0, 2, start=1, kind="text", text="hi"),)
    )
    with_overlay = timeline.Project(tracks=(*base.tracks, overlay))
    after = split_model.apply_segments(with_overlay, VIDEO, [SavedSegment(0.0, 8.0), SavedSegment(8.0, 21.0)])
    assert after.tracks[1] == overlay and len(after.tracks[0].clips) == 2

    mixed = timeline.Project(
        tracks=(replace_clips(base.tracks[0], (*base.tracks[0].clips, Clip("b", "other.mp4", 0, 3))), overlay)
    )
    with pytest.raises(ProjectError, match="compose editor"):
        split_model.apply_segments(mixed, VIDEO, [SavedSegment(0.0, 8.0)])


def replace_clips(track, clips):
    from dataclasses import replace

    return replace(track, clips=tuple(clips))


# --- opening and migrating, on both stores --------------------------------------------------


@pytest.mark.parametrize("name", FIXTURES)
def test_a_legacy_project_opens_as_the_same_segments_without_writing(env, probed, name):
    store, media, install, unified = env
    install(name)
    segments = expected_segments(name)

    saved = project.load_saved(store, VIDEO)
    assert saved.value == segments
    assert project.has_project(store, VIDEO) == bool(segments)
    assert project.current_revision(store, VIDEO) == store.load_split_versioned(VIDEO).revision
    shown = project.current_segments(store, media, VIDEO)
    assert shown == (segments or [SavedSegment(0.0, DURATION)])
    if unified is not None:
        assert not unified.exists()  # opening writes nothing


@pytest.mark.parametrize("name", FIXTURES)
def test_the_first_edit_writes_the_timeline_next_to_the_legacy_file_and_continues_the_revision(env, probed, name):
    store, media, install, unified = env
    install(name)
    before = project.current_revision(store, VIDEO)
    start = project.current_segments(store, media, VIDEO)

    saved = project.edit_segments(store, media, VIDEO, lambda cur: project.add_split(cur, 10.0)[0])
    assert saved.revision == before + 1
    assert project.current_revision(store, VIDEO) == before + 1
    assert project.current_segments(store, media, VIDEO) == saved.value
    assert len(saved.value) == len(start) + 1

    if unified is not None:
        data = json.loads(unified.read_text())
        assert data["version"] == 2 and data["revision"] == before + 1
        clips = data["tracks"][0]["clips"]
        assert [(c["in"], c["out"]) for c in clips] == [(s.start, s.end) for s in saved.value]
        legacy = unified.parent / f"{VIDEO}{SPLIT_SUFFIX}"
        assert legacy.read_text() == (LEGACY / f"{name}.split-video-project.json").read_text()  # untouched


def test_nothing_is_lost_in_the_migration_of_a_labelled_project(env, probed):
    store, media, install, unified = env
    install("v2_labelled")
    segments = expected_segments("v2_labelled")
    # a no-op edit still migrates: the whole legacy project is on the main track now
    saved = project.edit_segments(store, media, VIDEO, lambda cur: cur)
    assert saved.value == segments
    loaded = store.load_video_timeline(VIDEO).value
    assert loaded is not None
    clips = loaded.tracks[0].clips
    assert [(c.label, c.label_color, c.included, c.export_name) for c in clips] == [
        ("Opener", "#ff8800", True, None),
        ("Banter", None, False, None),
        ("Encore", "#00aaff", True, "The Encore"),
    ]
    assert all(c.source == VIDEO for c in clips)


def test_stale_writes_conflict_on_the_one_revision_number(env, probed):
    store, media, install, _ = env
    install("v2_labelled")
    current = project.current_revision(store, VIDEO)
    with pytest.raises(RevisionConflictError) as caught:
        project.save_segments(store, VIDEO, [SavedSegment(0.0, 5.0)], base_revision=current - 1)
    assert caught.value.current == current
    assert project.save_segments(store, VIDEO, [SavedSegment(0.0, 5.0)], base_revision=current) == current + 1
    with pytest.raises(RevisionConflictError):
        project.save_segments(store, VIDEO, [SavedSegment(0.0, 6.0)], base_revision=current)
    assert project.load_saved(store, VIDEO).value == [SavedSegment(0.0, 5.0)]


def test_a_failed_edit_writes_nothing(env, probed):
    store, media, install, unified = env
    install("v2_labelled")
    before = project.current_revision(store, VIDEO)

    def boom(_):
        raise ProjectError("no")

    with pytest.raises(ProjectError):
        project.edit_segments(store, media, VIDEO, boom)
    assert project.current_revision(store, VIDEO) == before
    assert project.load_saved(store, VIDEO).value == expected_segments("v2_labelled")


def test_a_fresh_video_has_nothing_saved_then_one_clip_per_segment(env, probed):
    store, media, _, unified = env
    assert project.load_saved(store, VIDEO) == project.load_saved(store, VIDEO)
    assert project.load_saved(store, VIDEO).value is None and project.current_revision(store, VIDEO) == 0
    assert project.current_segments(store, media, VIDEO) == [SavedSegment(0.0, DURATION)]
    revision = project.save_segments(
        store, VIDEO, [SavedSegment(0.0, 7.0), SavedSegment(7.0, 21.0, label="B")], media=media
    )
    assert revision == 1
    timeline_project = store.load_video_timeline(VIDEO).value
    assert timeline_project is not None and [c.label for c in timeline_project.tracks[0].clips] == ["", "B"]


def test_saving_no_segments_is_a_saved_project_with_none(env, probed):
    store, media, _, _ = env
    project.save_segments(store, VIDEO, [])
    assert project.load_saved(store, VIDEO).value == []
    assert not project.has_project(store, VIDEO)
    assert project.current_segments(store, media, VIDEO) == [SavedSegment(0.0, DURATION)]


def test_a_mixed_main_track_blocks_split_writes_but_not_reads(env, probed):
    store, media, _, _ = env
    project.save_segments(store, VIDEO, [SavedSegment(0.0, 21.0)])

    def mix(stored):
        track = stored.tracks[0]
        from dataclasses import replace

        return replace(
            stored, tracks=(replace(track, clips=(*track.clips, Clip("x", "other.mp4", 0, 3))), *stored.tracks[1:])
        )

    store.update_video_timeline(VIDEO, mix)
    with pytest.raises(ProjectError, match="compose editor"):
        project.edit_segments(store, media, VIDEO, lambda cur: cur)
    assert len(project.load_saved(store, VIDEO).value) == 2


# --- local store specifics ------------------------------------------------------------------


def test_the_video_timeline_is_the_timeline_named_after_the_video_but_not_listed(tmp_path, probed):
    root = tmp_path
    (root / VIDEO).write_bytes(b"x")
    (root / "reel.split-video-timeline.json").write_text(json.dumps(timeline.to_dict(timeline.empty_project())))
    store, media = LocalProjectStore(root), LocalMediaStore(root)
    project.save_segments(store, VIDEO, [SavedSegment(0.0, 5.0), SavedSegment(5.0, 21.0)], media=media)
    assert store.list_timelines() == ["reel"]
    # the compose side can still open it by name: one file, one revision
    loaded = service.load_existing(store, VIDEO)
    assert loaded.revision == 1 and len(loaded.tracks[0].clips) == 2
    assert store.peek_revisions([VIDEO])[("split", VIDEO)] == 1


def test_timeline_sits_in_the_videos_folder_and_ids_cannot_escape(tmp_path):
    (tmp_path / "gigs").mkdir()
    store = LocalProjectStore(tmp_path)
    store.update_video_timeline("gigs/a.mp4", lambda _: split_model.from_legacy("gigs/a.mp4", [SavedSegment(0.0, 2.0)]))
    assert (tmp_path / "gigs" / f"a.mp4{TIMELINE_SUFFIX}").exists()
    with pytest.raises(ValueError):
        store.update_video_timeline("../x.mp4", lambda _: timeline.empty_project())
    assert store.load_video_timeline("../x.mp4").value is None


def test_a_damaged_timeline_file_falls_back_to_the_legacy_project(tmp_path):
    (tmp_path / VIDEO).write_bytes(b"x")
    shutil.copy(LEGACY / "v2_labelled.split-video-project.json", tmp_path / f"{VIDEO}{SPLIT_SUFFIX}")
    (tmp_path / f"{VIDEO}{TIMELINE_SUFFIX}").write_text("{not json")
    store = LocalProjectStore(tmp_path)
    assert project.load_saved(store, VIDEO).value == expected_segments("v2_labelled")
    assert project.current_revision(store, VIDEO) == 5


def test_peeked_revision_follows_the_unified_file_once_it_exists(tmp_path, probed):
    (tmp_path / VIDEO).write_bytes(b"x")
    shutil.copy(LEGACY / "v2_labelled.split-video-project.json", tmp_path / f"{VIDEO}{SPLIT_SUFFIX}")
    store, media = LocalProjectStore(tmp_path), LocalMediaStore(tmp_path)
    assert store.peek_revisions([VIDEO])[("split", VIDEO)] == 5
    project.edit_segments(store, media, VIDEO, lambda cur: cur)
    assert store.peek_revisions([VIDEO])[("split", VIDEO)] == 6
