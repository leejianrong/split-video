"""Asset ids and the asset table, run against the in-memory stores."""

import json

import pytest

from split_video import assets, timeline, timeline_service
from split_video import timeline_ops as ops
from split_video.ffmpeg import MediaInfo
from split_video.media_store import InMemoryMediaStore, MediaStore
from split_video.store import InMemoryProjectStore
from split_video.timeline import (
    AssetNotFoundError,
    OutsideRootError,
    SourceNotFoundError,
    TimelineError,
    empty_project,
)

VIDEO = MediaInfo(duration=12.0, width=320, height=240, has_audio=True, fps=25.0)
MIB = 1 << 20


@pytest.fixture
def media():
    store = InMemoryMediaStore()
    store.add("a.mp4", b"a-bytes", info=VIDEO)
    store.add("sub/b.mp4", b"b-bytes", info=VIDEO)
    return store


def _id(media, path):
    return assets.register_path(media, path).id


# --- ids ---------------------------------------------------------------------------


def test_the_id_is_a_prefix_and_16_hex_digits(media):
    asset_id = _id(media, "a.mp4")
    assert assets.is_asset_id(asset_id) and len(asset_id) == 18 and asset_id.startswith("a_")
    assert not assets.is_asset_id("a.mp4") and not assets.is_asset_id("a_XYZ")


def test_same_bytes_at_another_path_or_name_give_the_same_id(media):
    media.add("other/renamed.mp4", b"a-bytes", info=VIDEO)
    assert _id(media, "other/renamed.mp4") == _id(media, "a.mp4") != _id(media, "sub/b.mp4")


def test_tiny_and_empty_files_are_hashed_whole(media):
    media.add("one.mp4", b"x", info=VIDEO)
    media.add("two.mp4", b"y", info=VIDEO)
    media.add("empty.mp4", b"", info=VIDEO)
    assert len({_id(media, p) for p in ("one.mp4", "two.mp4", "empty.mp4")}) == 3


def test_small_files_are_hashed_in_full_but_large_ones_by_samples(media):
    small = bytes(3 * MIB)
    media.add("small.mp4", small, info=VIDEO)
    media.add("small2.mp4", small[:1000] + b"!" + small[1001:], info=VIDEO)
    assert _id(media, "small.mp4") != _id(media, "small2.mp4")  # any byte counts up to 3 MiB

    big = bytearray(8 * MIB)
    media.add("big.mp4", bytes(big), info=VIDEO)
    outside = bytearray(big)
    outside[2 * MIB] = 1  # between the first and middle samples: not sampled (the documented trade-off)
    media.add("big_outside.mp4", bytes(outside), info=VIDEO)
    assert _id(media, "big.mp4") == _id(media, "big_outside.mp4")
    for name, offset in (("first", 10), ("middle", 4 * MIB), ("last", 8 * MIB - 10)):
        changed = bytearray(big)
        changed[offset] = 1
        media.add(f"big_{name}.mp4", bytes(changed), info=VIDEO)
        assert _id(media, f"big_{name}.mp4") != _id(media, "big.mp4"), name
    media.add("big_longer.mp4", bytes(big) + b"\0", info=VIDEO)  # the size is part of the id
    assert _id(media, "big_longer.mp4") != _id(media, "big.mp4")


# --- the table ---------------------------------------------------------------------------


def test_registering_records_stream_info_and_persists_the_table(media):
    record = assets.register_path(media, "a.mp4")
    assert (record.path, record.size, record.duration, record.has_audio) == ("a.mp4", 7, 12.0, True)
    assert (record.width, record.height, record.fps) == (320, 240, 25.0)
    saved = media.load_asset_table()
    assert saved["version"] == 1 and saved["assets"][record.id]["path"] == "a.mp4"
    assert [a.id for a in assets.list_assets(media)] == [record.id]
    assert json.loads(json.dumps(saved)) == saved


def test_registering_is_idempotent_and_skips_rehashing_unchanged_files(media, monkeypatch):
    first = assets.register_path(media, "a.mp4")
    hashed = []
    real = assets.sample_id
    monkeypatch.setattr(assets, "sample_id", lambda *a: hashed.append(a) or real(*a))
    assert assets.register_path(media, "a.mp4") == first and assets.register_path(media, "/a.mp4") == first
    assert hashed == []  # same path, size and mtime: straight from the table
    media.add("a.mp4", b"a-bytes", info=VIDEO, mtime=5.0)  # touched
    assert assets.register_path(media, "a.mp4").id == first.id and len(hashed) == 1
    assert len(assets.list_assets(media)) == 1


def test_two_copies_are_one_asset_whose_path_is_the_first(media):
    media.add("copy.mp4", b"a-bytes", info=VIDEO)
    first = assets.register_path(media, "a.mp4")
    copy = assets.register_path(media, "copy.mp4")
    assert copy.id == first.id and copy.path == "a.mp4" and copy.copies == {"copy.mp4": (7, 1.0)}
    assert len(assets.list_assets(media)) == 1
    assert assets.register_path(media, "copy.mp4") == copy  # and the copy isn't rehashed or re-recorded
    assert assets.list_assets(media)[0].summary(True)["copies"] == ["copy.mp4"]


def test_registering_a_bad_path_is_a_clear_error(media):
    with pytest.raises(SourceNotFoundError):
        assets.register_path(media, "nope.mp4")
    with pytest.raises(SourceNotFoundError):
        assets.register_path(media, "sub")  # a folder
    with pytest.raises(Exception, match="nope|escape|\\.\\."):
        assets.register_path(media, "../x.mp4")


def test_a_damaged_table_loads_as_empty_and_bad_rows_are_skipped(media):
    media.save_asset_table("garbage")
    assert assets.list_assets(media) == []
    good = assets.register_path(media, "a.mp4")
    table = media.load_asset_table()
    table["assets"]["a_0000000000000000"] = {"path": "x"}  # missing fields
    media.save_asset_table(table)
    assert [a.id for a in assets.list_assets(media)] == [good.id]


def test_the_in_memory_store_satisfies_the_interface_with_the_table_methods():
    assert isinstance(InMemoryMediaStore(), MediaStore)
    store = InMemoryMediaStore()
    assert store.load_asset_table() is None
    store.save_asset_table({"version": 1, "assets": {}})
    assert store.load_asset_table() == {"version": 1, "assets": {}}


# --- finding files again --------------------------------------------------------------------


def test_a_renamed_file_is_relinked_by_id_and_the_table_updated(media):
    record = assets.register_path(media, "a.mp4")
    media.rename("a.mp4", "moved/renamed.mp4")
    found = assets.get_asset(media, record.id)
    assert found.path == "moved/renamed.mp4" and found.id == record.id
    assert assets.list_assets(media)[0].path == "moved/renamed.mp4"  # persisted
    assert assets.register_path(media, "moved/renamed.mp4").id == record.id


def test_relinking_ignores_files_of_other_sizes_and_prefers_the_shallowest_match(media):
    record = assets.register_path(media, "a.mp4")
    media.rename("a.mp4", "deep/er/x.mp4")
    media.add("deep/y.mp4", b"a-bytes", info=VIDEO)
    media.add("z.mp4", b"a-bytes", info=VIDEO)
    media.add("decoy.mp4", b"a-byteZ", info=VIDEO)
    assert assets.get_asset(media, record.id).path == "z.mp4"


def test_a_file_replaced_in_place_is_not_the_asset_any_more(media):
    record = assets.register_path(media, "a.mp4")
    media.add("a.mp4", b"entirely different", info=VIDEO, mtime=9.0)
    with pytest.raises(SourceNotFoundError, match=record.id):
        assets.get_asset(media, record.id)
    assert assets.register_path(media, "a.mp4").id != record.id


def test_a_missing_asset_error_names_the_id_and_last_known_path(media):
    record = assets.register_path(media, "a.mp4")
    media.remove("a.mp4")
    with pytest.raises(SourceNotFoundError) as caught:
        assets.get_asset(media, record.id)
    assert record.id in str(caught.value) and "a.mp4" in str(caught.value)
    with pytest.raises(AssetNotFoundError, match="a_0123456789abcdef"):
        assets.get_asset(media, "a_0123456789abcdef")


def test_when_the_first_copy_is_deleted_the_asset_moves_to_the_other(media):
    media.add("copy.mp4", b"a-bytes", info=VIDEO)
    record = assets.register_path(media, "a.mp4")
    assets.register_path(media, "copy.mp4")
    media.remove("a.mp4")
    moved = assets.get_asset(media, record.id)
    assert moved.path == "copy.mp4" and moved.copies == {}


def test_a_lost_table_is_rebuilt_from_the_clips_source_hints(media):
    linked = assets.link_project(media, _project_with_sources("a.mp4"))
    assert linked.tracks[0].clips[0].asset == _id(media, "a.mp4")
    media.save_asset_table(None)  # the sidecar is deleted
    again = assets.link_project(media, linked)
    assert again == linked and [a.path for a in assets.list_assets(media)] == ["a.mp4"]
    # ... and so is a renamed file when the table is lost too
    media.rename("a.mp4", "new.mp4")
    media.save_asset_table(None)
    relinked = assets.link_project(media, linked)
    assert (
        relinked.tracks[0].clips[0].source == "new.mp4"
        and relinked.tracks[0].clips[0].asset == linked.tracks[0].clips[0].asset
    )


# --- linking projects ----------------------------------------------------------------------


def _project_with_sources(*sources):
    project = empty_project()
    for source in sources:
        project, _ = ops.add_clip(project, project.tracks[0].id, source, 12.0)
    return project


def test_linking_a_version_1_project_registers_each_distinct_source_once(media):
    project = _project_with_sources("a.mp4", "sub/b.mp4", "a.mp4")
    assert all(c.asset == "" for c in project.tracks[0].clips)
    linked = assets.link_project(media, project)
    clips = linked.tracks[0].clips
    assert clips[0].asset == clips[2].asset != clips[1].asset and all(assets.is_asset_id(c.asset) for c in clips)
    assert sorted(a.path for a in assets.list_assets(media)) == ["a.mp4", "sub/b.mp4"]
    assert assets.link_project(media, linked) == linked  # idempotent


def test_linking_keeps_a_clip_whose_file_is_missing_and_links_it_later(media):
    project = _project_with_sources("a.mp4", "gone.mp4")
    linked = assets.link_project(media, project)
    gone = linked.tracks[0].clips[1]
    assert gone.asset == "" and gone.source == "gone.mp4" and linked.tracks[0].clips[0].asset
    media.add("gone.mp4", b"back again", info=VIDEO)
    relinked = assets.link_project(media, linked)
    assert assets.is_asset_id(relinked.tracks[0].clips[1].asset)


def test_linking_leaves_text_clips_alone_and_tolerates_escaping_and_unreadable_sources(media):
    media.add("noinfo.mp4", b"no media info")
    project = _project_with_sources("../x.mp4", "noinfo.mp4")
    project, _ = ops.add_track(project, "video")
    project, _ = ops.add_text_clip(project, project.tracks[1].id, "Hi", 2.0)
    assert assets.link_project(media, project) == project


def test_link_project_refreshes_hints_for_a_moved_file(media):
    linked = assets.link_project(media, _project_with_sources("a.mp4"))
    media.rename("a.mp4", "elsewhere/a-renamed.mp4")
    moved = assets.link_project(media, linked)
    assert moved.tracks[0].clips[0].source == "elsewhere/a-renamed.mp4"
    assert moved.tracks[0].clips[0].asset == linked.tracks[0].clips[0].asset


def test_resolve_project_is_strict(media):
    linked = assets.link_project(media, _project_with_sources("a.mp4"))
    media.remove("a.mp4")
    with pytest.raises(SourceNotFoundError, match=linked.tracks[0].clips[0].asset):
        assets.resolve_project(media, linked)
    with pytest.raises(OutsideRootError):
        assets.resolve_project(media, _project_with_sources("../x.mp4"))
    with pytest.raises(TimelineError, match="not found"):
        assets.resolve_project(media, _project_with_sources("nope.mp4"))


# --- the file format and the service -------------------------------------------------------


def test_version_2_to_dict_writes_asset_and_source_and_loads_version_1_and_2(media):
    linked = assets.link_project(media, _project_with_sources("a.mp4"))
    data = timeline.to_dict(linked)
    clip = data["tracks"][0]["clips"][0]
    assert data["version"] == 2 and clip["asset"] == linked.tracks[0].clips[0].asset and clip["source"] == "a.mp4"
    assert timeline.from_dict(json.loads(json.dumps(data))) == linked
    del clip["asset"]  # a version 1 clip
    data["version"] = 1
    assert timeline.from_dict(data).tracks[0].clips[0].asset == ""


def test_text_clips_have_no_asset_in_the_file(media):
    project, track = ops.add_track(empty_project(), "video")
    project, _ = ops.add_text_clip(project, track.id, "Hi", 2.0)
    text = timeline.to_dict(project)["tracks"][1]["clips"][0]
    assert "asset" not in text and text["source"] == ""
    assert timeline.from_dict(timeline.to_dict(project)) == project


def test_service_adds_by_path_or_by_asset_id(media):
    store = InMemoryProjectStore()
    timeline_service.create_project(store, "p")
    by_path = timeline_service.add_clip(store, media, "p", "a.mp4", out_point=5.0)
    clip = by_path.tracks[0].clips[0]
    assert clip.source == "a.mp4" and assets.is_asset_id(clip.asset)

    by_id = timeline_service.add_clip(store, media, "p", asset=clip.asset, in_point=1.0, out_point=4.0)
    second = by_id.tracks[0].clips[1]
    assert second.asset == clip.asset and second.source == "a.mp4" and second.duration == 3.0

    for kwargs in ({}, {"path": "a.mp4", "asset": clip.asset}):
        with pytest.raises(TimelineError, match="exactly one"):
            timeline_service.add_clip(store, media, "p", kwargs.get("path"), asset=kwargs.get("asset"))
    with pytest.raises(AssetNotFoundError):
        timeline_service.add_clip(store, media, "p", asset="a_0123456789abcdef")


def test_a_clip_added_from_a_copy_keeps_the_path_it_was_added_with(media):
    media.add("copy.mp4", b"a-bytes", info=VIDEO)
    store = InMemoryProjectStore()
    timeline_service.create_project(store, "p")
    timeline_service.add_clip(store, media, "p", "a.mp4")
    project = timeline_service.add_clip(store, media, "p", "copy.mp4")
    first, second = project.tracks[0].clips
    assert first.asset == second.asset and (first.source, second.source) == ("a.mp4", "copy.mp4")
    described = timeline_service.describe(media, "p", project)["tracks"][0]["clips"]
    assert [c["source"] for c in described] == ["a.mp4", "copy.mp4"]


def test_describe_and_edits_follow_a_renamed_file(media):
    store = InMemoryProjectStore()
    timeline_service.create_project(store, "p")
    project = timeline_service.add_clip(store, media, "p", "a.mp4", out_point=5.0)
    clip = project.tracks[0].clips[0]
    media.rename("a.mp4", "renamed.mp4")

    shown = timeline_service.describe(media, "p", timeline_service.load_existing(store, "p", media))
    shown_clip = shown["tracks"][0]["clips"][0]
    assert shown_clip["source"] == "renamed.mp4" and shown_clip["asset"] == clip.asset
    assert shown_clip["source_duration"] == 12.0
    assert store.load_timeline("p").tracks[0].clips[0].source == "a.mp4"  # plain reads don't rewrite the file

    trimmed = timeline_service.trim_clip(store, media, "p", clip.id, out_point=8.0)
    assert trimmed.tracks[0].clips[0].source == "renamed.mp4"  # the edit saved the refreshed hint
    assert store.load_timeline("p").tracks[0].clips[0].source == "renamed.mp4"


def test_migrate_project_saves_version_2_now_and_the_first_edit_does_too(media):
    legacy = _project_with_sources("a.mp4", "gone.mp4")
    store = InMemoryProjectStore()
    store.save_timeline("old", legacy)
    store.save_timeline("old2", legacy)

    loaded = timeline_service.load_existing(store, "old", media)
    assert loaded.tracks[0].clips[0].asset and loaded.tracks[0].clips[1].asset == ""
    assert store.load_timeline("old") == legacy  # reading doesn't rewrite

    timeline_service.migrate_project(store, media, "old")
    saved = store.load_timeline("old")
    assert saved.tracks[0].clips[0].asset == loaded.tracks[0].clips[0].asset and saved.tracks[0].clips[1].asset == ""

    timeline_service.set_clip_properties(store, "old2", legacy.tracks[0].clips[0].id, media=media, gain=0.5)
    assert store.load_timeline("old2").tracks[0].clips[0].asset == loaded.tracks[0].clips[0].asset


def test_render_finds_a_renamed_source(media, monkeypatch):
    from split_video import render

    project = assets.link_project(media, _project_with_sources("a.mp4"))
    media.rename("a.mp4", "renamed.mp4")
    seen = {}
    monkeypatch.setattr(render, "_run_ffmpeg", lambda plan, inputs, *a: seen.update(inputs=inputs))
    render.render(project, media, "out.mp4")
    assert seen["inputs"][0].endswith("renamed.mp4")
